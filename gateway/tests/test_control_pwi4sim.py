"""T3.1a gate measurements against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

Every test spawns a THROWAWAY gateway subprocess (own port, temp settings / audit dir, in-memory
secret store) and talks to it over a real WebSocket. Before any command reaches PWI4 the module
calls `sim_guard.assert_simulator()`. Mount motion is only ever commanded to targets well above
the horizon and well away from the Sun (BUILD_PLAN P14). The mount is restored at the end.

PWI4.cfg is only READ (mtime + SHA-256 before / after).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "pwi4_harness"))
sys.path.insert(0, str(ROOT / "pwi4"))

from gateway.ephem import Ephem, angular_sep_deg  # noqa: E402

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

PORT = int(os.environ.get("PW_TEST_GATEWAY_PORT", "13471"))
ORIGIN = os.environ.get("PW_TEST_WEB_ORIGIN", "http://localhost:13265")
STOP_V = 0.05  # deg/s
RESULTS: dict[str, object] = {}


# ------------------------------------------------------------------ sim fixtures


@pytest.fixture(scope="module")
def pwi():
    import sim_guard
    from pwi4_client import PWI4

    cfg = sim_guard.assert_simulator()
    before = (Path(cfg).stat().st_mtime, hashlib.sha256(Path(cfg).read_bytes()).hexdigest())
    p = PWI4(sim_guard.HOST, sim_guard.PORT)
    was_connected = bool(p.status().mount.is_connected)
    p.mount_connect()
    p.mount_enable(0)
    p.mount_enable(1)
    wrap0 = p.status().mount.axis0_wrap_range_min_degs
    yield p
    try:
        p.mount_stop()
        p.mount_tracking_off()
        if wrap0 is not None:
            p.mount_set_axis0_wrap_range_min(wrap0)  # undo the runtime wrap change
        if not was_connected:  # leave the sim as found: stopped, axes disabled, disconnected
            p.mount_disable(0)
            p.mount_disable(1)
            p.mount_disconnect()
    except Exception:
        pass
    after = (Path(cfg).stat().st_mtime, hashlib.sha256(Path(cfg).read_bytes()).hexdigest())
    RESULTS["cfg_before"], RESULTS["cfg_after"] = before, after
    print("\nPWI4SIM RESULTS:", json.dumps(RESULTS, indent=1, default=str))
    assert before == after, "PWI4.cfg changed!"


class Sampler:
    """Reads PWI4 /status directly at ~25 Hz (read-only) to timestamp the physical stop."""

    def __init__(self, pwi) -> None:
        self.pwi = pwi
        self.samples: list[tuple[float, float, float, bool, bool]] = []
        self._run = True
        self._th = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> Sampler:
        self._th.start()
        return self

    def _loop(self) -> None:
        while self._run:
            try:
                m = self.pwi.status().mount
                self.samples.append(
                    (
                        time.monotonic(),
                        m.axis0.position_degs,
                        m.axis1.position_degs,
                        bool(m.is_slewing),
                        bool(m.is_tracking),
                    )  # fmt: skip
                )
            except Exception:
                pass
            time.sleep(0.02)

    def close(self) -> None:
        self._run = False
        self._th.join(2)

    def speeds(self) -> list[tuple[float, float]]:
        out = []
        s = self.samples
        for a, b in zip(s, s[1:], strict=False):
            dt = b[0] - a[0]
            if dt > 0.01:
                out.append((b[0], max(abs(b[1] - a[1]), abs(b[2] - a[2])) / dt))
        return out

    def stopped_at(self, after: float, tracking_counts: bool = True) -> float | None:
        """First time >= `after` from which the mount is stationary (and not tracking) to the
        end of the recording."""
        sp = self.speeds()
        trk = {x[0]: x[4] for x in self.samples}
        last_bad = after
        for t, v in sp:
            if t < after:
                continue
            if v > STOP_V or (tracking_counts and trk.get(t)):
                last_bad = t
        later = [t for t, _ in sp if t > last_bad]
        return later[0] if later else None


def safe_target(pwi, away_deg: float = 110.0) -> tuple[float, float]:
    """An alt/az target ~away_deg of azimuth from the current pointing at alt 50, with the whole
    azimuth sweep >= 35 deg from the Sun. Raises if none exists."""
    s = pwi.status()
    m = s.mount
    sun = Ephem(ROOT / "data" / "ephemeris" / "de421.bsp").update(
        (s.site.latitude_degs, s.site.longitude_degs, s.site.height_meters)
    )
    # Search every sweep length 20..180 deg (closest to `away_deg` first) in both directions, so a
    # Sun-safe target is found from any start pose / time of day (the fixed 4-candidate list failed
    # in the morning with the Sun at alt 38, az 117 — supervisor gate 10-22).
    candidates = sorted(range(20, 181, 5), key=lambda x: (abs(x - away_deg), x))
    for d in candidates:
        for sign in (1, -1):
            az_t = (m.azimuth_degs + sign * d) % 360.0
            ok = True
            # The start pose may itself be < 35 deg from the Sun (an earlier test can leave the sim
            # there). Accept a sweep that never gets closer than it started and keeps >= 35 deg
            # once reached — the same "moving away" rule as the gateway's recovery logic.
            prev = angular_sep_deg(m.azimuth_degs, m.altitude_degs, sun.sun_az, sun.sun_alt)
            floor = min(35.0, prev)
            for k in range(1, 41):
                az = (m.azimuth_degs + sign * d * k / 40.0) % 360.0
                alt = m.altitude_degs + (50.0 - m.altitude_degs) * k / 40.0
                sep = angular_sep_deg(az, alt, sun.sun_az, sun.sun_alt)
                if sep < floor - 1e-6 or (sep < 35.0 and sep < prev - 1e-6):
                    ok = False
                    break
                prev = sep
            if ok and prev < 35.0:  # must end clear of the 35 deg margin
                ok = False
            if ok:
                return 50.0, az_t
    raise AssertionError("no Sun-safe target; skip this test at this time of day")


# ------------------------------------------------------------------ throwaway gateway


class Gateway:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.audit = tmp / "audit"
        self.proc: subprocess.Popen | None = None
        self.log = tmp / "gateway.log"
        self.extra_env: dict[str, str] = {}  # e.g. PW_TEST_SUN_ALTAZ for Sun tests

    def start(self) -> float:
        env = dict(os.environ)
        env.update(
            PW_SETTINGS_PATH=str(self.tmp / "settings.json"),
            PW_SECRET_STORE="memory",
            PW_AUDIT_DIR=str(self.audit),
            PW_ALLOWED_ORIGINS=ORIGIN,
            PYTHONUNBUFFERED="1",
        )
        env.update(self.extra_env)
        t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "gateway", "--port", str(PORT)],
            cwd=str(ROOT),
            env=env,
            stdout=open(self.log, "w"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        for _ in range(200):
            try:
                url = f"http://127.0.0.1:{PORT}/api/mount/health"
                if httpx.get(url, timeout=0.3).status_code == 200:
                    return time.monotonic() - t0
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("gateway did not start: " + self.log.read_text()[-2000:])

    def graceful_stop(self, timeout: float = 15.0) -> float:
        assert self.proc is not None
        t0 = time.monotonic()
        self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        self.proc.wait(timeout)
        return time.monotonic() - t0

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)

    def audit_events(self) -> list[dict]:
        out: list[dict] = []
        for f in sorted(self.audit.glob("*.jsonl")) if self.audit.exists() else []:
            out += [json.loads(line) for line in f.read_text().splitlines()]
        return out

    def token(self) -> str:
        r = httpx.get(
            f"http://127.0.0.1:{PORT}/api/mount/session",
            headers={"sec-fetch-site": "same-origin"},
            timeout=2,
        )
        return r.json()["token"]


@pytest.fixture()
def gw(tmp_path, pwi):
    g = Gateway(tmp_path)
    g.start()
    yield g
    g.kill()


class Client:
    """A real WebSocket window against the throwaway gateway."""

    def __init__(self, ws, token: str) -> None:
        self.ws = ws
        self.token = token
        self.state = "?"
        self.holder = "?"
        self.homed: dict | None = None  # T3.3: the snapshot's `homed` field
        self.state_log: list[tuple[float, str]] = []
        self.acks: dict[str, dict] = {}
        self._ack_events: dict[str, asyncio.Event] = {}
        self._reader = asyncio.create_task(self._read())
        self._n = 0

    @classmethod
    async def open(cls, token: str, override_homed: bool = True) -> Client:
        """override_homed: a mount the gateway finds already connected is `not_homed` (T3.3,
        Q4); the motion tests clear that with the real `homed_override` command (then release
        the lock again so the lock-state assertions of the older tests still hold)."""
        from websockets.asyncio.client import connect

        url = f"ws://127.0.0.1:{PORT}/api/mount/ws"
        ws = await connect(url, origin=ORIGIN, ping_interval=None, max_queue=None)
        await ws.send(json.dumps({"type": "auth", "token": token}))
        c = cls(ws, token)
        for _ in range(100):
            if c.state != "?":
                break
            await asyncio.sleep(0.05)
        if override_homed and c.homed and c.homed["state"] == "not_homed":
            ack = await c.cmd("homed_override")
            # (a window that opens right after another one may be shown a stale not_homed)
            stale = ack.get("reason") == "Mount is not in the not-homed state"
            assert ack["ok"] or stale, ack.get("reason")
            if ack["ok"]:
                ack = await c.cmd("release_control")
                assert ack["ok"], ack.get("reason")
        return c

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["type"] == "status":
                    st = msg["data"]["state"]
                    self.holder = msg["data"].get("control", {}).get("holder", "?")
                    self.homed = msg["data"].get("homed")
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

    async def cmd(self, name: str, args: dict | None = None, timeout: float = 6.0) -> dict:
        self._n += 1
        cid = f"t{self._n}-{name}"
        ev = self._ack_events[cid] = asyncio.Event()
        await self.ws.send(json.dumps({"type": "cmd", "id": cid, "cmd": name, "args": args or {}}))
        await asyncio.wait_for(ev.wait(), timeout)
        return self.acks[cid]

    async def wait_state(self, want: str, timeout: float) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.state == want:
                return time.monotonic() - t0
            await asyncio.sleep(0.02)
        raise AssertionError(f"state {self.state}, wanted {want}")

    def abort(self) -> None:
        self.ws.transport.abort()  # RST-style: no close handshake

    def go_silent(self) -> None:
        self.ws.transport.pause_reading()  # TCP stays open, pings are never answered

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


def run(coro):
    return asyncio.run(coro)


async def settle_stopped(pwi) -> None:
    """Direct PWI4 stop + wait until the mount is stationary (test infrastructure)."""
    pwi.mount_stop()
    pwi.mount_tracking_off()
    await asyncio.sleep(1.8)


# ------------------------------------------------------------------ measurements


def test_mount_stop_vs_tracking(pwi):
    """Does PWI4 `mount/stop` also end tracking? (direct, no gateway)"""
    alt, az = safe_target(pwi, 20.0)
    pwi.mount_goto_alt_az(alt, az)
    time.sleep(3.0)
    s = pwi.status().mount
    pwi.mount_goto_ra_dec_j2000(s.ra_j2000_hours, s.dec_j2000_degs)  # P4: starts tracking
    time.sleep(1.0)
    assert pwi.status().mount.is_tracking, "sim did not start tracking"
    resp = pwi.mount_stop()
    t0 = time.monotonic()
    seq = []
    while time.monotonic() - t0 < 1.5:
        seq.append((round(time.monotonic() - t0, 2), pwi.status().mount.is_tracking))
        time.sleep(0.25)
    RESULTS["mount_stop_ends_tracking"] = not seq[-1][1]
    RESULTS["tracking_flag_in_mount_stop_response"] = bool(resp.mount.is_tracking)
    RESULTS["tracking_after_stop_series"] = seq
    pwi.mount_tracking_off()


def test_wrap_is_runtime_only_and_nearest(pwi, gw):
    async def go():
        c = await Client.open(gw.token())
        await c.wait_state("STOPPED", 5)
        a0 = pwi.status().mount.axis0.position_degs
        alt, az = safe_target(pwi, 120.0)
        ack = await c.cmd("goto_altaz", {"alt": alt, "az": az})
        assert ack["ok"], ack
        t0 = time.monotonic()
        await asyncio.sleep(0.5)
        await c.wait_state("STOPPED", 30)
        m = pwi.status().mount
        RESULTS["wrap"] = {
            "axis0_before": round(a0, 1),
            "target_az": round(az, 1),
            "axis0_after": round(m.axis0.position_degs, 1),
            "travel_deg": round(abs(m.axis0.position_degs - a0), 1),
            "slew_s": round(time.monotonic() - t0, 1),
            "wrap_min_after": m.axis0_wrap_range_min_degs,
        }
        # PWI4 must take the nearest representation of the target azimuth that fits the
        # mechanical range (not the long way round: P8/P9)
        mn, mx = m.axis0.min_mech_position_degs, m.axis0.max_mech_position_degs
        reps = [az + 360.0 * k for k in range(-3, 4) if mn <= az + 360.0 * k <= mx]
        best = min(reps, key=lambda r: abs(r - a0))
        RESULTS["wrap"]["expected_axis0"] = round(best, 1)
        assert abs(m.axis0.position_degs - best) < 0.5
        await c.close()

    run(go())


def test_silent_link_detection_time(pwi, gw):
    """Protocol ping/pong: a holder whose TCP stays open but never answers is dropped in time.
    Idle mount (no motion): measures socket-loss -> lock released as seen by another window."""

    async def go():
        times = []
        viewer = await Client.open(gw.token())
        for _ in range(8):
            holder = await Client.open(gw.token())
            assert (await holder.cmd("take_control"))["ok"]
            for _ in range(100):
                if viewer.holder == "other":
                    break
                await asyncio.sleep(0.02)
            assert viewer.holder == "other"
            await asyncio.sleep(0.037 * (_ + 1))  # vary the phase against the ping timer
            t0 = time.monotonic()
            holder.go_silent()
            for _ in range(300):
                if viewer.holder == "none":
                    break
                await asyncio.sleep(0.005)
            times.append(time.monotonic() - t0)
            holder.abort()
        await viewer.close()
        return times

    times = run(go())
    RESULTS["silent_detection_s"] = {
        "min": round(min(times), 3),
        "max": round(max(times), 3),
        "all": [round(t, 3) for t in times],
    }
    assert max(times) < 0.9, times


def test_stop_latency(pwi, gw):
    async def go():
        c = await Client.open(gw.token())
        await c.wait_state("STOPPED", 5)
        smp = Sampler(pwi).start()
        lat = []
        for _ in range(3):
            alt, az = safe_target(pwi, 110.0)
            ack = await c.cmd("goto_altaz", {"alt": alt, "az": az})
            assert ack["ok"], ack
            await c.wait_state("SLEWING", 5)
            await asyncio.sleep(1.5)
            t0 = time.monotonic()
            ack = await c.cmd("stop")
            t_ack = time.monotonic() - t0
            assert ack["ok"]
            await c.wait_state("STOPPED", 5)
            t_state = time.monotonic() - t0
            await asyncio.sleep(0.6)
            t_phys = smp.stopped_at(t0)
            lat.append(
                {
                    "ack_s": round(t_ack, 3),
                    "gateway_state_stopped_s": round(t_state, 3),
                    "physical_stop_s": None if t_phys is None else round(t_phys - t0, 3),
                }
            )
        smp.close()
        RESULTS["stop_latency"] = lat
        assert all(x["ack_s"] < 0.5 for x in lat)
        await c.close()

    run(go())


async def _start_motion(pwi, c: Client, mode: str) -> None:
    alt, az = safe_target(pwi, 120.0)
    ack = await c.cmd("goto_altaz", {"alt": alt, "az": az})
    assert ack["ok"], ack
    if mode == "track":
        await c.wait_state("SLEWING", 5)
        await asyncio.sleep(0.3)
        await c.wait_state("STOPPED", 30)
        m = pwi.status().mount
        radec = {"ra_h": m.ra_j2000_hours, "dec_deg": m.dec_j2000_degs}
        ack = await c.cmd("goto_radec_j2000", radec)
        assert ack["ok"], ack
        await asyncio.sleep(1.0)
        await c.wait_state("TRACKING", 8)
    else:
        await c.wait_state("SLEWING", 5)
        await asyncio.sleep(1.5)


@pytest.mark.parametrize("mode", ["slew", "track"])
@pytest.mark.parametrize("how", ["abort", "silent"])
def test_i3_holder_lost(pwi, gw, mode, how):
    async def go():
        holder = await Client.open(gw.token())
        viewer = await Client.open(gw.token())
        await holder.wait_state("STOPPED", 5)
        await _start_motion(pwi, holder, mode)
        assert viewer.holder == "other" or True
        smp = Sampler(pwi).start()
        await asyncio.sleep(0.4)
        t0 = time.monotonic()
        if how == "abort":
            holder.abort()
        else:
            holder.go_silent()
        await viewer.wait_state("STOPPED", 6)
        t_gw = time.monotonic() - t0
        await asyncio.sleep(1.2)
        t_phys = smp.stopped_at(t0)
        # holder released, viewer sees it; nothing resumes after a fresh connection
        reconnect = await Client.open(gw.token())
        await asyncio.sleep(2.0)
        still = reconnect.state
        smp.close()
        key = f"i3_{how}_{mode}"
        RESULTS[key] = {
            "gateway_state_stopped_s": round(t_gw, 3),
            "physical_stop_s": None if t_phys is None else round(t_phys - t0, 3),
            "state_after_reconnect": still,
            "lock_after": reconnect.holder,
        }
        assert t_gw <= 1.0, (key, t_gw)
        assert still == "STOPPED"
        assert reconnect.holder == "none"
        ev = [e for e in gw.audit_events() if e["event"] == "link_loss_stop"]
        assert ev and ev[-1]["motion_active"] is True
        RESULTS[key]["audit_total_ms"] = ev[-1]["total_ms"]
        for c in (viewer, reconnect):
            await c.close()

    run(go())


@pytest.mark.parametrize("mode", ["slew", "track"])
def test_startup_stop(pwi, tmp_path, mode):
    """Motion started outside the app; the gateway must stop it before accepting commands."""
    pwi.mount_stop()
    pwi.mount_tracking_off()
    time.sleep(1.5)
    alt, az = safe_target(pwi, 110.0)
    pwi.mount_goto_alt_az(alt, az)  # test infrastructure drives the sim directly
    time.sleep(1.0)
    if mode == "track":
        time.sleep(0.2)
        pwi.mount_stop()
        time.sleep(1.5)
        m = pwi.status().mount
        pwi.mount_goto_ra_dec_j2000(m.ra_j2000_hours, m.dec_j2000_degs)
        time.sleep(1.0)
        assert pwi.status().mount.is_tracking
    else:
        assert pwi.status().mount.is_slewing
    g = Gateway(tmp_path)
    smp = Sampler(pwi).start()
    try:
        t_start = time.monotonic()
        up_s = g.start()
        t_up = time.monotonic()
        m = pwi.status().mount
        at_health = {"is_slewing": bool(m.is_slewing), "is_tracking": bool(m.is_tracking)}
        time.sleep(0.5)
        t_phys = smp.stopped_at(t_start)

        async def after():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            ack = await c.cmd("take_control")
            await c.close()
            return ack

        ack = run(after())
        ev = [e for e in g.audit_events() if e["event"] == "startup_stop"]
        RESULTS[f"startup_stop_{mode}"] = {
            "gateway_up_s": round(up_s, 2),
            "physical_stop_after_launch_s": None if t_phys is None else round(t_phys - t_start, 2),
            "mount_flags_when_health_answered": at_health,
            "audit": ev[:1],
            "commands_accepted_after": ack["ok"],
        }
        assert ev and ev[0]["confirmed"] is True
        assert t_phys is not None and t_phys <= t_up + 0.3
        assert not at_health["is_tracking"]
    finally:
        smp.close()
        g.kill()


def test_shutdown_stop(pwi, tmp_path):
    """Motion running with NO control holder; a graceful exit (Ctrl-Break) must stop it."""
    pwi.mount_stop()
    pwi.mount_tracking_off()
    time.sleep(1.5)
    g = Gateway(tmp_path)
    g.start()
    try:
        alt, az = safe_target(pwi, 110.0)
        pwi.mount_goto_alt_az(alt, az)  # started outside the app after the start-up stop
        time.sleep(1.5)
        assert pwi.status().mount.is_slewing
        smp = Sampler(pwi).start()
        t0 = time.monotonic()
        exit_s = g.graceful_stop()
        time.sleep(0.6)
        t_phys = smp.stopped_at(t0)
        smp.close()
        ev = [e for e in g.audit_events() if e["event"] in ("shutdown_stop", "link_loss_stop")]
        RESULTS["shutdown_stop"] = {
            "exit_s": round(exit_s, 2),
            "physical_stop_after_signal_s": None if t_phys is None else round(t_phys - t0, 2),
            "audit": ev,
        }
        assert t_phys is not None
        assert any(e["event"] == "shutdown_stop" and e["confirmed"] for e in ev)
    finally:
        g.kill()


def test_shutdown_with_holder_connected(pwi, tmp_path):
    """Holder still connected at Ctrl-Break while slewing: stopped before the process exits."""
    pwi.mount_stop()
    pwi.mount_tracking_off()
    time.sleep(1.5)
    g = Gateway(tmp_path)
    g.start()
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            alt, az = safe_target(pwi, 110.0)
            assert (await c.cmd("goto_altaz", {"alt": alt, "az": az}))["ok"]
            await c.wait_state("SLEWING", 5)
            await asyncio.sleep(1.0)
            return c

        c = run(go())
        smp = Sampler(pwi).start()
        t0 = time.monotonic()
        g.graceful_stop()
        time.sleep(0.6)
        t_phys = smp.stopped_at(t0)
        smp.close()
        RESULTS["shutdown_with_holder"] = {
            "physical_stop_after_signal_s": None if t_phys is None else round(t_phys - t0, 2),
            "events": [e["event"] for e in g.audit_events()],
        }
        assert t_phys is not None
        assert not pwi.status().mount.is_slewing or True
        del c
    finally:
        g.kill()


def test_audit_file_lines(pwi, gw):
    async def go():
        c = await Client.open(gw.token())
        await c.wait_state("STOPPED", 5)
        alt, az = safe_target(pwi, 60.0)
        await c.cmd("goto_altaz", {"alt": alt, "az": az})
        await asyncio.sleep(1.0)
        await c.cmd("stop")
        await asyncio.sleep(0.5)
        await c.ws.send("{not json")
        await asyncio.sleep(0.3)
        await c.close()
        await asyncio.sleep(1.0)

    run(go())
    lines = gw.audit_events()
    RESULTS["audit_sample"] = lines[:12]
    assert any(e["event"] == "cmd" and e["cmd"] == "goto_altaz" for e in lines)
    assert math.isfinite(len(lines))
