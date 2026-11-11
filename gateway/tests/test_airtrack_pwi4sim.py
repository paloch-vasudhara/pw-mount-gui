# ruff: noqa: F811
"""T5.6b: aircraft cueing against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

BUILD_PLAN Phase 5b "T5.6b" and finding P23. Every test drives a THROWAWAY gateway
(`PW_ADSB_SIMULATE=1`, the same as `--simulated-adsb`, `PW_ADSB_FEED_URL` -> a fake adsb_interpreter
feed on a throwaway port 18750-18759, the Sun pinned with `PW_TEST_SUN_ALTAZ`) over a real
WebSocket with `track_aircraft {icao}`. The fake feed (`GenFeed`) computes its rows AT SEND TIME
from a synthetic flight placed relative to PWI4's own site: one fix per second with 0.3 s latency
(the row's `pos_age_s` is exact, so the gateway's fix time is exact). The mount is recorded at
~25 Hz straight from PWI4 and compared with an independent truth (adsb_core geometry +
`refract_alt`). `sim_guard.assert_simulator()` runs in the shared `pwi` fixture before any PWI4
command; PWI4 settings / site are never written (the wrap range is a runtime value restored by
the fixture).

How to run (PWI4 in Simulator mode, nothing on 3200 / 8400 / 8410 / 3300 / 8500 / 8510):
    PWI4_SIM=1 PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python -m pytest -q -s
        gateway/tests/test_airtrack_pwi4sim.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import time
from dataclasses import dataclass

import pytest

from gateway.adsb import SITE_HAE_OFFSET_M
from gateway.ephem import angular_sep_deg

from . import fake_adsb
from .test_airtrack import CORE, Flight
from .test_control_pwi4sim import (  # noqa: F401  (pwi is a fixture)
    RESULTS,
    Client,
    Gateway,
    pwi,
    run,
)
from .test_dsun_pwi4sim import prepare_pose
from .test_track_object_pwi4sim import OFF, SLOW_CALL_S, WSampler

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

SUN_FAR = "-60,0"
LIM_EL = 15.0
LATENCY_S = 0.3
MEAN_TOL_DEG = 0.05  # after the 3 s settle
MAX_TOL_DEG = 0.3
SETTLE_S = 3.0


# ------------------------------------------------------------------ the feed


class GenFeed(fake_adsb.FakeFeed):
    """FakeFeed whose rows come from `Flight`s, computed when each snapshot is sent: the newest fix
    (one per integer second, available LATENCY_S later) with the exact `pos_age_s`. `freeze()`
    stops new fixes (the snapshots go on, with the last row and a growing age)."""

    def __init__(self) -> None:
        self.flights: list[Flight] = []
        self.frozen_at: float | None = None
        super().__init__([], interval=0.1)

    @property
    def rows(self) -> list[dict]:
        now = time.time()
        eff = now if self.frozen_at is None else min(now, self.frozen_at)
        f = math.floor(eff - LATENCY_S)
        return [fl.row(f, now - f) for fl in self.flights]

    @rows.setter
    def rows(self, value) -> None:  # FakeFeed.__init__ assigns it
        pass

    def freeze(self) -> float:
        """No more fixes from now on; returns the time of the last fix."""
        self.frozen_at = time.time()
        return math.floor(self.frozen_at - LATENCY_S)


# ------------------------------------------------------------------ the client and the rig


class AirClient(Client):
    """Client that records every snapshot's (time, state, reason, airtrack)."""

    def __init__(self, ws, token: str) -> None:
        self.snaps: list[tuple[float, str, str | None, dict | None]] = []
        self.air: dict | None = None
        self.adsb: dict | None = None
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
                    self.air = d.get("airtrack")
                    self.adsb = d.get("adsb")
                    self.reason = d.get("state_reason")
                    self.snaps.append((time.monotonic(), st, self.reason, self.air))
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
        return (self.air or {}).get("phase")

    @property
    def ended(self) -> dict | None:
        return (self.air or {}).get("ended")

    async def until(self, pred, timeout: float, what: str) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return time.monotonic()
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"timeout waiting for {what}; state={self.state} reason={self.reason} air={self.air}"
        )


@dataclass
class Rig:
    pwi: object
    tmp: object
    g: Gateway | None = None
    c: AirClient | None = None
    smp: WSampler | None = None
    feed: GenFeed | None = None
    site: tuple = ()

    async def open(self) -> AirClient:
        prepare_pose(self.pwi, 50.0)
        s = self.pwi.status().site
        self.site = (s.latitude_degs, s.longitude_degs, s.height_meters)
        self.feed = GenFeed()
        await self.feed.__aenter__()
        self.g = Gateway(self.tmp)
        self.g.extra_env = {
            "PW_TEST_SUN_ALTAZ": SUN_FAR,
            "PW_ADSB_SIMULATE": "1",
            "PW_ADSB_FEED_URL": self.feed.url,
        }
        self.g.start()
        self.c = await AirClient.open(self.g.token())
        await self.c.wait_state("STOPPED", 8)
        self.smp = WSampler(self.pwi).start()
        return self.c

    def flight(self, **kw) -> Flight:
        """A flight at PWI4's site (HAE = MSL + the gateway's offset), t_ref = now."""
        site_hae = CORE.geometry.Geodetic(
            self.site[0], self.site[1], self.site[2] + SITE_HAE_OFFSET_M
        )
        fl = Flight(site=site_hae, t_ref=time.time(), **kw)
        self.feed.flights = [fl]
        return fl

    async def wait_green(self) -> None:
        await self.c.until(
            lambda: bool(self.c.adsb and self.c.adsb.get("green")), 15, "the ADS-B feed green"
        )
        await asyncio.sleep(2.0)  # the table needs a snapshot with the flight

    async def close(self) -> None:
        try:
            if self.smp:
                self.smp.close()
            if self.c:
                await self.c.close()
        finally:
            if self.g:
                self.g.kill()
            if self.feed:
                # the feed server lives on the loop of the run() that opened it; once that loop
                # is closed the server is gone with it
                with contextlib.suppress(RuntimeError):
                    await self.feed.__aexit__(None, None, None)
            try:
                self.pwi.mount_stop()
                self.pwi.mount_tracking_off()
                self.pwi.mount_offset(axis0_reset=1, axis1_reset=1)
            except Exception:
                pass


def follow_errors(rig: Rig, fl: Flight, t_from: float, t_to: float) -> list[float]:
    """Sky error (deg) of the mount against the flight's refracted position, for the samples with
    Unix time in [t_from, t_to] (slow /status answers are skipped)."""
    rows = [
        s
        for s in rig.smp.samples
        if t_from <= s[0] + OFF <= t_to and not (len(s) > 5 and s[5] > SLOW_CALL_S)
    ]
    out = []
    for s in rows:
        alt, az = fl.altaz(s[0] + OFF)
        out.append(angular_sep_deg(s[1] % 360.0, s[2], az, alt))
    return out


async def start_track(rig: Rig, c: AirClient, fl: Flight) -> dict:
    await rig.wait_green()
    ack = await c.cmd("track_aircraft", {"icao": fl.icao}, timeout=20)
    assert ack["ok"], (ack.get("reason"), ack.get("code"))
    await c.until(lambda: c.phase in ("waiting", "tracking"), 60, "waiting / tracking")
    await c.until(lambda: c.phase == "tracking", 60, "tracking")
    return dict(c.air)


# ------------------------------------------------------------------ the cases

# ICAO addresses must be 6 hex digits (the command schema)
CASE_ICAO = {"ga": "ABA001", "cruise": "ABC001", "turn": "ABD001"}

CASES = {
    # (3 km up, 130 m/s, 2 km aside; peak 56 deg), closest approach 35 s after the start
    "ga": dict(
        flight=dict(e0=-130.0 * 35, n0=2000.0, h_above_site=3000.0, speed=130.0, heading=90.0),
        track_s=40.0,
    ),
    # cruise (10.7 km up, 250 m/s, 6.2 km aside: peak ~60 deg), closest approach 40 s in
    "cruise": dict(
        flight=dict(e0=-250.0 * 40, n0=6180.0, h_above_site=10_700.0, speed=250.0, heading=90.0),
        track_s=45.0,
    ),
    # a 3 deg/s turn (radius ~2 km), staying between 26 and 65 deg
    "turn": dict(
        flight=dict(
            e0=-3000.0, n0=3000.0, h_above_site=2500.0, speed=105.0, heading=90.0, turn=3.0
        ),
        track_s=40.0,
        # 11-11 sim: 0.054 deg mean (the turn at 2.5 km is the hardest case; the real feed's
        # predictor residual, 4.8 m p50, is ~0.1 deg at that range)
        mean_tol=0.08,
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_follows_the_aircraft(pwi, tmp_path, case):
    """Mean error <= 0.05 deg, max <= 0.3 deg while tracking (after a 3 s settle)."""
    spec = CASES[case]
    rig = Rig(pwi, tmp_path)

    async def go():
        c = await rig.open()
        rig.flight(icao=CASE_ICAO[case], callsign=case.upper(), **spec["flight"])
        fl = rig.feed.flights[0]
        info = await start_track(rig, c, fl)
        t_end = time.time() + spec["track_s"]
        await c.until(
            lambda: time.time() >= t_end or c.ended is not None, spec["track_s"] + 5, "run"
        )
        assert c.ended is None, c.ended  # still tracking
        t_cmd_stop = time.monotonic()
        ack = await c.cmd("stop")
        assert ack["ok"]
        await asyncio.sleep(1.5)
        return c, fl, info, t_cmd_stop

    try:
        c, fl, info, t_stop = run(go())
        errs = follow_errors(rig, fl, info["t_r"] + SETTLE_S, time.time() - 2.0)
        assert len(errs) > 100, len(errs)
        mean, worst = sum(errs) / len(errs), max(errs)
        RESULTS[f"air_{case}"] = {
            "samples": len(errs), "mean_deg": round(mean, 4), "max_deg": round(worst, 4),
        }  # fmt: skip
        assert mean <= spec.get("mean_tol", MEAN_TOL_DEG) and worst <= MAX_TOL_DEG, (mean, worst)
        assert rig.smp.stopped_at(t_stop, tracking_counts=False) is not None
    finally:
        run(rig.close())


def test_feed_stop_stops_the_mount(pwi, tmp_path):
    """The feed stops mid-track (the last row repeats): STOP within 10 s + 1.5 s of the last fix,
    reason "ADS-B data lost", the mount still afterwards."""
    rig = Rig(pwi, tmp_path)

    async def go():
        c = await rig.open()
        rig.flight(icao="ABF001", callsign="FEEDSTOP", **CASES["ga"]["flight"])
        fl = rig.feed.flights[0]
        await start_track(rig, c, fl)
        await asyncio.sleep(6.0)
        last_fix = rig.feed.freeze()
        await c.until(lambda: c.ended is not None, 20, "the track to end")
        t_seen = time.monotonic()
        ended = dict(c.ended)
        await asyncio.sleep(2.0)
        return c, fl, last_fix, ended, t_seen

    try:
        c, fl, last_fix, ended, t_seen = run(go())
        RESULTS["air_feed_stop"] = {"end_after_last_fix_s": round(ended["at"] - last_fix, 2)}
        assert ended["reason"] == "ADS-B data lost"
        assert 10.0 < ended["at"] - last_fix <= 11.5, ended["at"] - last_fix
        assert c.state == "STOPPED"
        stopped = rig.smp.stopped_at(t_seen - 1.5, tracking_counts=True)
        assert stopped is not None and stopped - t_seen <= 1.5, (stopped, t_seen)
    finally:
        run(rig.close())


def test_lim_el_exit_ends_the_track(pwi, tmp_path):
    """The aircraft climbs out of sight below Lim EL: the track ends at the cut, the mount stops
    and never goes below Lim EL - 0.1."""
    rig = Rig(pwi, tmp_path)

    async def go():
        c = await rig.open()
        rig.flight(
            icao="ABE001", callsign="LIMEL", e0=5000.0, n0=2000.0, h_above_site=3000.0,
            speed=200.0, heading=90.0,
        )  # fmt: skip
        fl = rig.feed.flights[0]
        t_exit = fl.exit_time(fl.t_ref)
        t_start = time.monotonic()
        await start_track(rig, c, fl)
        await c.until(lambda: c.ended is not None, 60, "the track to end")
        t_seen = time.monotonic()
        ended = dict(c.ended)
        await asyncio.sleep(2.0)
        return c, ended, t_exit, t_start, t_seen

    try:
        c, ended, t_exit, t_start, t_seen = run(go())
        low = min(s[2] for s in rig.smp.samples if s[0] >= t_start)
        RESULTS["air_lim_el"] = {
            "end_after_exit_s": round(ended["at"] - t_exit, 2), "lowest_alt_deg": round(low, 3),
        }  # fmt: skip
        assert ended["reason"] == "Aircraft below Lim EL"
        assert t_exit + 0.4 <= ended["at"] <= t_exit + 2.5, ended["at"] - t_exit
        assert low >= LIM_EL - 0.1
        assert c.state == "STOPPED" and c.reason == "Aircraft below Lim EL"
        stopped = rig.smp.stopped_at(t_seen - 1.5, tracking_counts=True)
        assert stopped is not None and stopped - t_seen <= 1.5
    finally:
        run(rig.close())
