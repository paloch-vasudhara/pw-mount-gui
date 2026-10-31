# ruff: noqa: F811
"""T4.2d: object track workflow against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

BUILD_PLAN section 6 "T4.2 / 5. Sim tests". Every test generates a synthetic LEO pass over the
simulator's own site (`tlegen.gen_tle`, epoch around now, ~10 s each), serves it from a TLE stub
(`tle_stub.TleStub`) and drives a THROWAWAY gateway (`PW_ANALYSIS_URL` -> the stub, the Sun pinned
with `PW_TEST_SUN_ALTAZ`) over a real WebSocket with `track_object {norad}`. The mount is recorded
at ~25 Hz straight from PWI4 and compared with an independent skyfield + `refract_alt` truth.
`sim_guard.assert_simulator()` runs in the shared `pwi` fixture before any PWI4 command. PWI4
settings / site are never written (the wrap range is a runtime value restored by the fixture).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pytest

from gateway.ephem import angular_sep_deg
from gateway.transforms import refract_alt

from .test_control_pwi4sim import (  # noqa: F401  (pwi is a fixture)
    RESULTS,
    Client,
    Gateway,
    Sampler,
    pwi,
    run,
)
from .test_dsun_pwi4sim import prepare_pose
from .test_safety_pwi4sim import set_lim_el
from .tle_stub import TleStub
from .tlegen import gen_tle

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

SUN_FAR = "-60,0"
LIM_EL = 15.0
GEN_S = 10.0  # about how long gen_tle takes (the pass timing is set for the END of the generation)
STOP_LIMIT_S = 0.3
FOLLOW_TOL_DEG = 0.05
SLOW_CALL_S = 0.15  # a /status call slower than this gives an unreliable time stamp (skipped)
# the sampler and the gateway snapshots use time.monotonic(); the truth uses Unix time
OFF = time.time() - time.monotonic()


# ------------------------------------------------------------------ fixtures / passes


@pytest.fixture(scope="module")
def stub():
    with TleStub() as s:
        yield s


@pytest.fixture(scope="module")
def site(pwi):
    s = pwi.status().site
    return (s.latitude_degs, s.longitude_degs, s.height_meters)


@dataclass
class Pass:
    norad: int
    name: str
    line1: str
    line2: str
    t_cross: float  # Unix: the upward 15 deg crossing
    t_peak: float
    peak: float


_NORAD = [99100]


def make_pass(stub: TleStub, site, peak: float, cross_in: float, name: str) -> Pass:
    """A synthetic pass at the sim site whose 15 deg crossing is `cross_in` seconds after the END
    of this call (negative: already up). Registered in the stub under a fresh NORAD id."""
    _NORAD[0] += 1
    norad = _NORAD[0]
    lead = 60.0
    epoch = time.time() + GEN_S + cross_in - lead
    r = gen_tle(peak, lead, site, epoch, name=name, satnum=norad % 100000)
    t_cross = epoch + float(r["fast"]["t_in"])
    stub.set(
        norad,
        name,
        r["line1"],
        r["line2"],
        epoch=datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        age_days=0.0,
    )
    return Pass(
        norad,
        name,
        r["line1"],
        r["line2"],
        t_cross,
        epoch + float(r["fast"]["t_peak"]),
        float(r["fast"]["peak"]),
    )


def truth(p: Pass, site, t_unix) -> tuple[np.ndarray, np.ndarray]:
    """(refracted alt, az) of the pass's object at Unix times (independent skyfield path)."""
    from skyfield.api import EarthSatellite, load, wgs84

    ts = load.timescale()
    sat = EarthSatellite(p.line1, p.line2, p.name, ts)
    obs = wgs84.latlon(site[0], site[1], elevation_m=site[2])
    tu = np.asarray(t_unix, dtype=float)
    d = datetime.fromtimestamp(float(tu.min()), UTC)
    day0 = datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()
    t = ts.utc(d.year, d.month, d.day, 0, 0, tu - day0)
    alt, az, _ = (sat - obs).at(t).altaz()
    return np.array([refract_alt(float(a)) for a in alt.degrees]), np.asarray(az.degrees)


def obj_at(p: Pass, site, t_unix: float) -> tuple[float, float]:
    alt, az = truth(p, site, [t_unix])
    return float(alt[0]), float(az[0])


# ------------------------------------------------------------------ recording


class WSampler(Sampler):
    """Sampler that also records the axis-0 wrap minimum with every sample."""

    def __init__(self, pwi) -> None:
        super().__init__(pwi)
        self.wraps: list[tuple[float, float]] = []

    def _loop(self) -> None:
        while self._run:
            try:
                # midpoint of the call + its duration: a slow /status answer (load) must not count
                # as a pointing error (Phase 4 end: 0.4-0.6 deg spikes in the full suite only)
                t_a = time.monotonic()
                m = self.pwi.status().mount
                t_b = time.monotonic()
                t = 0.5 * (t_a + t_b)
                self.samples.append(
                    (
                        t,
                        m.axis0.position_degs,
                        m.axis1.position_degs,
                        bool(m.is_slewing),
                        bool(m.is_tracking),
                        t_b - t_a,
                    )  # fmt: skip
                )
                self.wraps.append((t, float(m.axis0_wrap_range_min_degs)))
            except Exception:
                pass
            time.sleep(0.02)

    def wrap_at(self, t: float) -> float:
        return min(self.wraps, key=lambda w: abs(w[0] - t))[1]


class TrackClient(Client):
    """Client that records every snapshot's (time, state, reason, track)."""

    def __init__(self, ws, token: str) -> None:
        self.snaps: list[tuple[float, str, str | None, dict | None]] = []
        self.track: dict | None = None
        self.reason: str | None = None
        super().__init__(ws, token)

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["type"] == "status":
                    d = msg["data"]
                    st = d["state"]
                    self.holder = d.get("control", {}).get("holder", "?")
                    self.homed = d.get("homed")
                    self.track = d.get("track")
                    self.reason = d.get("state_reason")
                    self.snaps.append((time.monotonic(), st, self.reason, self.track))
                    if st != self.state:
                        self.state_log.append((time.monotonic(), st))
                    self.state = st
                elif msg["type"] == "ack":
                    self.acks[msg["id"]] = msg
                    ev = self._ack_events.get(msg["id"])
                    if ev:
                        ev.set()
        except Exception:
            pass

    @property
    def phase(self) -> str | None:
        return (self.track or {}).get("phase")

    @property
    def ended(self) -> dict | None:
        return (self.track or {}).get("ended")

    async def until(self, pred, timeout: float, what: str) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return time.monotonic()
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"timeout waiting for {what}; state={self.state} reason={self.reason} "
            f"track={self.track}"
        )

    def first(self, pred) -> float | None:
        for t, st, rs, tr in self.snaps:
            if pred(st, rs, tr):
                return t
        return None


def events(g: Gateway, *names: str) -> list[dict]:
    return [e for e in g.audit_events() if e["event"] in names]


class Rig:
    """Mount prepared (stopped, alt 50), throwaway gateway, client, sampler; cleaned up after."""

    def __init__(self, pwi, tmp_path, stub: TleStub, sun: str = SUN_FAR) -> None:
        self.pwi = pwi
        self.tmp = tmp_path
        self.stub = stub
        self.sun = sun
        self.g: Gateway | None = None
        self.c: TrackClient | None = None
        self.smp: WSampler | None = None

    async def open(self, lim: float | None = None, pose_az: float | None = None) -> TrackClient:
        prepare_pose(self.pwi, 50.0)
        if pose_az is not None:  # put the mount at alt 50 / the given azimuth
            self.pwi.mount_goto_alt_az(50.0, pose_az)
            await asyncio.sleep(1.0)
            for _ in range(100):
                m = self.pwi.status().mount
                if not m.is_slewing:
                    break
                await asyncio.sleep(0.3)
        self.g = Gateway(self.tmp)
        self.g.extra_env = {"PW_TEST_SUN_ALTAZ": self.sun, "PW_ANALYSIS_URL": self.stub.url}
        self.g.start()
        if lim is not None:
            set_lim_el(self.g, lim)
        self.c = await TrackClient.open(self.g.token())
        await self.c.wait_state("STOPPED", 8)
        self.smp = WSampler(self.pwi).start()
        await asyncio.sleep(0.3)
        return self.c

    def run(self, coro):
        """Run a test coroutine; on any error clean the rig up (gateway process!) and re-raise."""
        try:
            return run(coro)
        except BaseException:
            run(self.close())
            raise

    def _dump_trace(self) -> None:
        _dump_trace_impl(self)

    async def close(self) -> None:
        try:
            if self.smp:
                self.smp.close()
                self._dump_trace()
            if self.c:
                await self.c.close()
        finally:
            if self.g:
                self.g.kill()
            try:
                self.pwi.mount_stop()
                self.pwi.mount_tracking_off()
                self.pwi.mount_offset(axis0_reset=1, axis1_reset=1)
            except Exception:
                pass


def _dump_trace_impl(rig: Rig) -> None:
    """Debug aid: PW_T42D_TRACE=<file> appends a 1 s trace (unix, axis0, axis1) of the run."""
    path = os.environ.get("PW_T42D_TRACE")
    if not path or rig.smp is None:
        return
    with open(path, "a") as f:
        f.write(f"# {rig.tmp.name}\n")
        last = -9.0
        for s in rig.smp.samples:
            if s[0] - last >= 1.0:
                f.write(f"{s[0] + OFF:.1f} {s[1]:.2f} {s[2]:.2f} {int(s[3])} {int(s[4])}\n")
                last = s[0]


def follow_errors(smp: WSampler, p: Pass, site, t_from: float, t_to: float):
    """[(t_unix, error_deg)] of the mount against the object's refracted position, for samples
    with Unix time in [t_from, t_to]."""
    rows = [s for s in smp.samples if t_from <= s[0] + OFF <= t_to]
    slow = [s for s in rows if len(s) > 5 and s[5] > SLOW_CALL_S]
    RESULTS.setdefault("slow_status_samples_skipped", []).append(len(slow))
    rows = [s for s in rows if not (len(s) > 5 and s[5] > SLOW_CALL_S)]
    if not rows:
        return []
    tu = np.array([s[0] + OFF for s in rows])
    alt, az = truth(p, site, tu)
    return [
        (float(t), angular_sep_deg(s[1] % 360.0, s[2], float(a_z), float(a_l)))
        for t, s, a_l, a_z in zip(tu, rows, alt, az, strict=True)
    ]


def speed_between(smp: Sampler, t0: float, t1: float) -> float:
    """Highest mount speed (deg/s, either axis) between two monotonic times."""
    sp = [v for t, v in smp.speeds() if t0 <= t <= t1]
    return max(sp) if sp else 0.0


def min_alt(smp: Sampler, t0: float, t1: float) -> float:
    return min(s[2] for s in smp.samples if t0 <= s[0] <= t1)


def stop_latency(smp: Sampler, t_cmd: float) -> tuple[float | None, float | None]:
    a = smp.stopped_at(t_cmd, tracking_counts=False)
    b = smp.stopped_at(t_cmd, tracking_counts=True)
    return (a - t_cmd if a else None, b - t_cmd if b else None)


def now_s() -> float:
    return time.time()


# ------------------------------------------------------------------ tests


def test_normal_pass(pwi, stub, site, tmp_path):
    """Row 1: the object is already above Lim EL; follows it to Lim EL and stops there."""
    p = make_pass(stub, site, 20.0, -20.0, "SYN-NORMAL")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        t_cmd = time.monotonic()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.until(lambda: c.phase in ("waiting", "tracking"), 60, "waiting/tracking")
        info = dict(c.track)
        t_r, t_end = info["t_r"], info["t_end"]
        assert t_r - now_s() < 40
        await c.until(lambda: c.ended is not None, max(t_end - now_s(), 0) + 25, "track ended")
        t_seen = c.first(lambda st, rs, tr: tr is not None and tr.get("ended") is not None)
        await asyncio.sleep(1.5)
        return c, info, t_cmd, t_seen

    c, info, t_cmd, t_seen = rig.run(go())
    try:
        t_r, t_end = info["t_r"], info["t_end"]
        errs = follow_errors(rig.smp, p, site, t_r + 3.0, t_end - 0.5)
        assert len(errs) > 100, len(errs)
        max_err = max(e for _, e in errs)
        lowest = min_alt(rig.smp, t_cmd, time.monotonic())
        end_delay = t_seen + OFF - t_end
        phases = []
        for _, _, _, tr in c.snaps:
            ph = (tr or {}).get("phase")
            if ph and (not phases or phases[-1] != ph):
                phases.append(ph)
        ev_names = [e["event"] for e in events(rig.g, "track_start", "track_done", "track_stopped")]
        RESULTS["normal"] = {
            "t_r_minus_now_at_ack_s": round(t_r - (t_cmd + OFF), 1),
            "t_end_minus_t_r_s": round(t_end - t_r, 1),
            "follow_err_max_deg": round(max_err, 4),
            "min_alt_deg": round(lowest, 3),
            "end_delay_after_t_end_s": round(end_delay, 2),
            "ended": c.ended,
            "final_state": c.state,
            "phases": phases,
            "audit": ev_names,
        }
        assert max_err <= FOLLOW_TOL_DEG
        assert lowest >= LIM_EL - 0.1
        assert c.state == "STOPPED"
        assert c.ended["reason"] == "Track ended at Lim EL"
        assert -0.5 <= end_delay <= 2.0, end_delay
        assert ev_names == ["track_start", "track_done"], ev_names
        assert phases[-1] == "tracking"
    finally:
        run(rig.close())


def test_rising_below_limit_waits(pwi, stub, site, tmp_path):
    """Row 2: rises 90 s ahead; SLEWING -> TRACKING 'Waiting' still at the crossing point until
    t_r, then follows; never below Lim EL - 0.1."""
    p = make_pass(stub, site, 30.0, 90.0, "SYN-RISING")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        wrap_before = rig.pwi.status().mount.axis0_wrap_range_min_degs
        t_cmd = time.monotonic()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.until(lambda: c.phase == "slewing" or c.phase == "waiting", 10, "slewing")
        t_wait = await c.until(lambda: c.phase == "waiting", 60, "waiting")
        info = dict(c.track)
        wait_state = (c.state, c.reason)
        t_r = info["t_r"]
        await c.until(lambda: c.phase == "tracking", max(t_r - now_s(), 0) + 10, "tracking")
        t_track = time.monotonic()
        await asyncio.sleep(max(t_r + 30.0 - now_s(), 5.0))
        t_stop = time.monotonic()
        ack = await c.cmd("stop")
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.wait_state("STOPPED", 5)
        await asyncio.sleep(0.8)
        return c, info, t_cmd, t_wait, t_track, t_stop, wait_state, wrap_before

    c, info, t_cmd, t_wait, t_track, t_stop, wait_state, wrap_before = rig.run(go())
    try:
        t_r = info["t_r"]
        t_r_mono = t_r - OFF
        # the mount stands still between "waiting" (+ a settle) and t_r
        still_speed = speed_between(rig.smp, t_wait + 0.5, t_r_mono - 0.3)
        # the position held is the crossing point of the pass (refracted)
        alt_c, az_c = obj_at(p, site, t_r)
        pos = min(rig.smp.samples, key=lambda s: abs(s[0] - (t_r_mono - 0.3)))
        pos_err = angular_sep_deg(pos[1] % 360.0, pos[2], az_c, alt_c)
        errs = follow_errors(rig.smp, p, site, t_r + 3.0, t_stop + OFF - 0.3)
        max_err = max(e for _, e in errs)
        lowest = min_alt(rig.smp, t_cmd, t_stop)
        phases = []
        for _, _, _, tr in c.snaps:
            ph = (tr or {}).get("phase")
            if ph and (not phases or phases[-1] != ph):
                phases.append(ph)
        wrap_wait = rig.smp.wrap_at(t_wait + 0.5)
        wrap_end = rig.smp.wrap_at(t_stop)
        RESULTS["rising"] = {
            "t_r_minus_t_cross_s": round(t_r - p.t_cross, 2),
            "waiting_s": round(t_r_mono - t_wait, 1),
            "still_max_speed_deg_s": round(still_speed, 4),
            "pos_err_at_t_r_deg": round(pos_err, 4),
            "follow_err_max_deg": round(max_err, 4),
            "min_alt_deg": round(lowest, 3),
            "state_while_waiting": wait_state,
            "phases": phases,
            "wrap_before_cmd": wrap_before,
            "wrap_after_upload": wrap_wait,
            "wrap_end": wrap_end,
        }
        assert wait_state[0] == "TRACKING" and wait_state[1].startswith("Waiting for")
        assert phases[:3] == ["slewing", "waiting", "tracking"] or phases[:2] == [
            "waiting",
            "tracking",
        ]
        assert t_r_mono - t_wait > 5.0
        assert still_speed < 0.05
        assert pos_err < 0.1
        assert max_err <= FOLLOW_TOL_DEG
        assert lowest >= LIM_EL - 0.1
        assert wrap_end == wrap_wait
    finally:
        run(rig.close())


def test_below_horizon_lead_waits_and_tracks(pwi, stub, site, tmp_path):
    """Q27b: the object is still below 0 deg, its Lim EL crossing ~4 min ahead (inside the 5 min
    lead): slew to the crossing point, wait, track, end "Track ended at Lim EL"."""
    p = make_pass(stub, site, 20.0, 255.0, "SYN-LEAD")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        alt_cmd, _ = obj_at(p, site, now_s())
        t_cmd = time.monotonic()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        t_wait = await c.until(lambda: c.phase == "waiting", 60, "waiting")
        info = dict(c.track)
        wait_state = (c.state, c.reason)
        await c.until(lambda: c.ended is not None, max(info["t_end"] - now_s(), 0) + 25, "ended")
        t_seen = c.first(lambda st, rs, tr: tr is not None and tr.get("ended") is not None)
        await asyncio.sleep(1.0)
        return c, info, alt_cmd, t_cmd, t_wait, t_seen, wait_state

    c, info, alt_cmd, t_cmd, t_wait, t_seen, wait_state = rig.run(go())
    try:
        t_r, t_end = info["t_r"], info["t_end"]
        t_r_mono = t_r - OFF
        errs = follow_errors(rig.smp, p, site, t_r + 3.0, t_end - 0.5)
        max_err = max(e for _, e in errs)
        lowest = min_alt(rig.smp, t_cmd, time.monotonic())
        phases = []
        for _, _, _, tr in c.snaps:
            ph = (tr or {}).get("phase")
            if ph and (not phases or phases[-1] != ph):
                phases.append(ph)
        RESULTS["lead_below_horizon"] = {
            "object_alt_at_command_deg": round(alt_cmd, 2),
            "t_r_minus_t_cross_s": round(t_r - p.t_cross, 2),
            "waiting_s": round(t_r_mono - t_wait, 1),
            "follow_err_max_deg": round(max_err, 4),
            "min_alt_deg": round(lowest, 3),
            "phases": phases,
            "ended": c.ended,
        }
        assert alt_cmd < 0.0, alt_cmd
        assert wait_state[0] == "TRACKING" and wait_state[1].startswith("Waiting for")
        assert phases[:3] == ["slewing", "waiting", "tracking"], phases
        assert abs(t_r - p.t_cross) < 8.0
        assert t_r_mono - t_wait > 5.0
        assert len(errs) > 100 and max_err <= FOLLOW_TOL_DEG
        assert lowest >= LIM_EL - 0.1
        assert c.state == "STOPPED" and c.ended["reason"] == "Track ended at Lim EL"
    finally:
        run(rig.close())


def test_peak_below_limit_is_refused(pwi, stub, site, tmp_path):
    """Row 3: a 30 deg pass with Lim EL raised to 40: refused `peak_below_limit`, nothing sent."""
    p = make_pass(stub, site, 30.0, 30.0, "SYN-LOW")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open(lim=40.0)
        await asyncio.sleep(1.0)
        pos0 = rig.pwi.status().mount
        a0 = (pos0.axis0.position_degs, pos0.axis1.position_degs)
        n0 = len(rig.smp.samples)
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        await asyncio.sleep(2.0)
        return c, ack, a0, n0

    c, ack, a0, n0 = rig.run(go())
    try:
        post = rig.smp.samples[n0:]
        drift = max(max(abs(s[1] - a0[0]), abs(s[2] - a0[1])) for s in post)
        slewed = any(s[3] or s[4] for s in post)
        RESULTS["peak_below"] = {
            "ack": {k: ack.get(k) for k in ("ok", "reason", "code")},
            "max_drift_deg": round(drift, 4),
            "slewing_or_tracking_seen": slewed,
            "snapshot_track": c.track,
            "state": c.state,
            "audit": [e["event"] for e in events(rig.g, "track_start")],
        }
        assert not ack["ok"] and ack.get("code") == "peak_below_limit", ack
        assert drift < 0.01 and not slewed
        assert c.track is None and c.state == "STOPPED"
        assert not events(rig.g, "track_start")
    finally:
        run(rig.close())


def test_through_the_sun_detours(pwi, stub, site, tmp_path):
    """Row 4: the Sun pinned on the pass 20 s after its peak: 'Avoiding the Sun', separation >= 15
    deg, back on the object within 3 s of t_b. The pass rises 90 s ahead so the Sun point always
    lies after the rendezvous; pinned "75 s from now" it could fall before t_r when the rig was slow
    to start, and the planner then (correctly) moved the rendezvous past the interval: no detour."""
    p = make_pass(stub, site, 30.0, 90.0, "SYN-SUN")
    t_sun = p.t_peak + 20.0
    sun_alt, sun_az = obj_at(p, site, t_sun)
    sun = f"{sun_alt:.4f},{sun_az:.4f}"
    rig = Rig(pwi, tmp_path, stub, sun=sun)

    async def go():
        # a start pose well away from the Sun
        c = await rig.open(pose_az=(sun_az + 150.0) % 360.0)
        t_cmd = time.monotonic()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.until(lambda: c.phase in ("waiting", "tracking"), 60, "tracking")
        info = dict(c.track)
        assert info["detours"], info
        t_b = info["detours"][-1]["t_b"]
        await c.until(
            lambda: c.phase == "detour",
            max(info["detours"][0]["t_a"] - now_s(), 0) + 15,
            "detour phase",
        )
        await c.until(
            lambda: c.phase == "tracking" and now_s() > t_b,
            max(t_b - now_s(), 0) + 15,
            "back to tracking",
        )
        await asyncio.sleep(max(t_b + 9.0 - now_s(), 0.5))
        t_stop = time.monotonic()
        ack = await c.cmd("stop")
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.wait_state("STOPPED", 5)
        return c, info, t_cmd, t_stop

    c, info, t_cmd, t_stop = rig.run(go())
    try:
        det = info["detours"][0]
        t_a, t_b = det["t_a"], info["detours"][-1]["t_b"]
        seps = [
            (s[0], angular_sep_deg(s[1] % 360.0, s[2], sun_az, sun_alt))
            for s in rig.smp.samples
            if t_cmd <= s[0] <= t_stop
        ]
        min_sep = min(v for _, v in seps)
        sep_in = min(v for t, v in seps if t_a - 3 <= t + OFF <= t_b + 3)
        errs = follow_errors(rig.smp, p, site, t_b + 3.0, t_stop + OFF - 0.3)
        back_err = max(e for _, e in errs)
        saw_reason = any(rs == "Avoiding the Sun" for _, _, rs, _ in c.snaps)
        ev = events(rig.g, "track_start")[0]
        RESULTS["sun"] = {
            "sun_altaz": sun,
            "detours": info["detours"],
            "detour_len_s": round(t_b - t_a, 1),
            "plan_min_sun_sep": ev.get("min_sun_sep"),
            "min_sun_sep_measured_deg": round(min_sep, 3),
            "min_sun_sep_in_detour_deg": round(sep_in, 3),
            "follow_err_max_3s_after_t_b_deg": round(back_err, 4),
            "reason_seen": saw_reason,
        }
        assert saw_reason
        assert min_sep >= 15.0, min_sep
        assert (ev.get("min_sun_sep") or 99) >= 15.5
        assert back_err <= FOLLOW_TOL_DEG
    finally:
        run(rig.close())


def test_stop_mid_track(pwi, stub, site, tmp_path):
    """Row 5: STOP while following: stopped within 0.3 s, STOPPED, `track_stopped`."""
    p = make_pass(stub, site, 30.0, -20.0, "SYN-STOP")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.until(lambda: c.phase == "tracking", 60, "tracking")
        await asyncio.sleep(12.0)
        t_cmd = time.monotonic()
        ack = await c.cmd("stop")
        t_ack = time.monotonic() - t_cmd
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.wait_state("STOPPED", 3)
        await asyncio.sleep(2.5)
        return c, t_cmd, t_ack

    c, t_cmd, t_ack = rig.run(go())
    try:
        lat_phys, lat_trk = stop_latency(rig.smp, t_cmd)
        evs = events(rig.g, "track_start", "track_stopped", "track_done")
        RESULTS["stop"] = {
            "ack_s": round(t_ack, 3),
            "stopped_s": None if lat_phys is None else round(lat_phys, 3),
            "stopped_and_not_tracking_s": None if lat_trk is None else round(lat_trk, 3),
            "state": c.state,
            "track": c.track,
            "audit": [(e["event"], e.get("reason")) for e in evs],
        }
        assert lat_phys is not None and lat_phys <= STOP_LIMIT_S, lat_phys
        assert c.state == "STOPPED"
        assert [e["event"] for e in evs] == ["track_start", "track_stopped"]
        assert c.phase is None
    finally:
        run(rig.close())


def test_goto_replaces_track(pwi, stub, site, tmp_path):
    """Row 6: Go To during a track ends it (`track_replaced`) and the goto runs."""
    p = make_pass(stub, site, 30.0, -20.0, "SYN-GOTO")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.until(lambda: c.phase == "tracking", 60, "tracking")
        await asyncio.sleep(8.0)
        m = rig.pwi.status().mount
        tgt_az = (m.azimuth_degs + 100.0) % 360.0
        ack = await c.cmd("goto_altaz", {"alt": 55.0, "az": tgt_az}, timeout=10)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.wait_state("SLEWING", 5)
        await c.wait_state("STOPPED", 40)
        await asyncio.sleep(1.5)
        return c, tgt_az

    c, tgt_az = rig.run(go())
    try:
        m = pwi.status().mount
        err = angular_sep_deg(m.azimuth_degs, m.altitude_degs, tgt_az, 55.0)
        evs = events(rig.g, "track_start", "track_replaced", "track_stopped", "track_done")
        RESULTS["goto"] = {
            "final_err_deg": round(err, 4),
            "state": c.state,
            "track": c.track,
            "audit": [(e["event"], e.get("reason")) for e in evs],
        }
        assert err < 0.2
        assert c.state == "STOPPED" and c.phase is None
        assert [e["event"] for e in evs] == ["track_start", "track_replaced"]
        assert not pwi.status().mount.is_tracking
    finally:
        run(rig.close())


def test_wrap_unchanged_by_the_track(pwi, stub, site, tmp_path):
    """Row 7: after the upload the axis-0 wrap minimum stays what the executor set (the path has
    update_wrap=0, so PWI4 does not rewrite it)."""
    p = make_pass(stub, site, 30.0, -20.0, "SYN-WRAP")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        wrap_before = rig.pwi.status().mount.axis0_wrap_range_min_degs
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        t_up = await c.until(lambda: c.phase in ("waiting", "tracking"), 60, "uploaded")
        await c.until(lambda: c.phase == "tracking", 40, "tracking")
        await asyncio.sleep(25.0)
        t_end = time.monotonic()
        ack = await c.cmd("stop")
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.wait_state("STOPPED", 5)
        return wrap_before, t_up, t_end

    wrap_before, t_up, t_end = rig.run(go())
    try:
        vals = [w for t, w in rig.smp.wraps if t_up + 0.3 <= t <= t_end]
        RESULTS["wrap"] = {
            "wrap_before_cmd": wrap_before,
            "wrap_after_upload": vals[0],
            "wrap_min_over_track": min(vals),
            "wrap_max_over_track": max(vals),
            "samples": len(vals),
        }
        assert len(vals) > 200
        assert max(vals) - min(vals) < 1e-6
    finally:
        run(rig.close())


def test_second_track_replaces_the_first(pwi, stub, site, tmp_path):
    """Row 8 (G5): a second `track_object` ends the first and starts the second."""
    p = make_pass(stub, site, 30.0, -20.0, "SYN-A")
    stub.set(
        p.norad + 500, "SYN-B", p.line1, p.line2, epoch=None, age_days=0.0
    )  # the same orbit under a second id
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.until(lambda: c.phase == "tracking", 60, "tracking A")
        await asyncio.sleep(5.0)
        ack2 = await c.cmd("track_object", {"norad": p.norad + 500}, timeout=20)
        assert ack2["ok"], (ack2.get("reason"), ack2.get("code"))
        await c.until(
            lambda: (c.track or {}).get("norad") == p.norad + 500 and c.phase == "tracking",
            60,
            "tracking B",
        )
        await asyncio.sleep(6.0)
        ack = await c.cmd("stop")
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.wait_state("STOPPED", 5)
        return c

    rig.run(go())
    try:
        evs = events(rig.g, "track_start", "track_replaced", "track_stopped", "track_done")
        RESULTS["replace"] = {
            "audit": [(e["event"], e.get("norad"), e.get("reason")) for e in evs],
        }
        names = [(e["event"], e["norad"]) for e in evs]
        assert names == [
            ("track_start", p.norad),
            ("track_replaced", p.norad),
            ("track_start", p.norad + 500),
            ("track_stopped", p.norad + 500),
        ], names
    finally:
        run(rig.close())


# 89 deg tripped the axis-space corridor (2.02 vs 2.0) before Q19: tracks now measure it on the sky
@pytest.mark.parametrize("peak", [88.0, 89.0])
def test_near_zenith_pass(pwi, stub, site, tmp_path, peak):
    """Row 9: peak 88 / 89 deg: the axis-0 lag near the peak does not trip the corridor; back on the
    object within 5 s after the peak; the card note is in the snapshot."""
    p = make_pass(stub, site, peak, -100.0, "SYN-ZENITH")
    rig = Rig(pwi, tmp_path, stub)

    async def go():
        c = await rig.open()
        ack = await c.cmd("track_object", {"norad": p.norad}, timeout=20)
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.until(lambda: c.phase in ("waiting", "tracking"), 60, "uploaded")
        info = dict(c.track)
        t_peak = info["t_peak"]
        assert t_peak - now_s() > 8, "the peak came too early to measure"
        await c.until(lambda: c.phase == "tracking", 40, "tracking")
        await asyncio.sleep(max(t_peak + 14.0 - now_s(), 0.5))
        still = c.state == "TRACKING" and c.phase in ("tracking", "detour")
        t_stop = time.monotonic()
        ack = await c.cmd("stop")
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        await c.wait_state("STOPPED", 5)
        return c, info, still, t_stop

    c, info, still, t_stop = rig.run(go())
    try:
        t_peak = info["t_peak"]
        errs = follow_errors(rig.smp, p, site, info["t_r"] + 2.0, t_stop + OFF - 0.3)
        around = [(t, e) for t, e in errs if t >= t_peak]
        peak_err = max(e for _, e in errs)
        # first time after the peak from which the error stays <= 0.05
        back = None
        for i, (t, _) in enumerate(around):
            if all(e <= FOLLOW_TOL_DEG for _, e in around[i:]):
                back = t - t_peak
                break
        evs = events(rig.g, "track_start", "track_stopped", "track_done")
        stops_before_cmd = [
            1
            for t, st, rs, tr in c.snaps
            if st == "STOPPED" and t < t_stop and t > c.snaps[0][0] + 5
        ]
        RESULTS[f"zenith_{peak:.0f}"] = {
            "peak_alt_pass_deg": round(p.peak, 2),
            "peak_alt_snapshot": info["peak_alt"],
            "notes": info["notes"],
            "peak_error_deg": round(peak_err, 3),
            "back_within_0.05_s_after_peak": None if back is None else round(back, 1),
            "still_tracking_before_stop": still,
            "audit": [(e["event"], e.get("reason")) for e in evs],
        }
        assert info["notes"] and "Near-zenith" in info["notes"][0]
        assert still
        assert [e["event"] for e in evs] == ["track_start", "track_stopped"]
        assert evs[1]["reason"] != "Track ended at Lim EL"
        # an 89 deg transit needs ~180 deg of axis 0 at the 20 deg/s cap (~9 s) while the object
        # turns in ~3-4 s, so its recovery is longer (Q18 accepts the lag; Q19 keeps tracking)
        assert back is not None and back <= (5.0 if peak < 88.5 else 8.0), back
        assert not stops_before_cmd
    finally:
        run(rig.close())
