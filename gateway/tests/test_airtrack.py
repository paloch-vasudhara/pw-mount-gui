"""T5.6b: aircraft cueing (airtrack.py) against FakePwi4 and synthetic overflights.

No PWI4, no ADS-B platform and no 8500 feed are contacted: the aircraft come from a fake runner
(`FakeRunner`: green / detail / core / range_limit_m / aircraft_for) whose records are built with
`adapt_feed_aircraft` from `fake_adsb.feed_row` rows, i.e. the same `adsb_core.state.Aircraft` the
simulated source hands to `predict.path`. Time is a FAKE clock: `ControlService.air_clock` /
`air_sleep` are replaced by the `Rig` below, which also plays the feed (one fix per second, 0.3 s
latency) and the mount (parked at the rendezvous, optionally following the uploaded path), so a
2-minute overflight runs in a few seconds.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading

import pytest

from gateway import airtrack as at
from gateway import state
from gateway.adsb import SITE_HAE_OFFSET_M, adapt_feed_aircraft, load_core
from gateway.airtrack import AirChecker
from gateway.commands import CommandError, validate_command
from gateway.ephem import angular_sep_deg
from gateway.pwi4_io import parse
from gateway.transforms import refract_alt

from . import fake_adsb
from .conftest import FakePwi4, status_text
from .test_check import check
from .test_control import Win, audit_lines, running, wait_for
from .test_dsun_wiring import park_fake, snap_when
from .test_track_exec import Rig as SatRig
from .test_track_exec import events, paths, uploads
from .test_track_exec import row as tle_row
from .tle_stub import TleStub

adsb_core = pytest.importorskip("adsb_core")
pytestmark = pytest.mark.skipif(
    not str(adsb_core.__version__).startswith("0.3."), reason="needs adsb_core 0.3.x"
)

CORE, _WHY = load_core()
GEO = CORE.geometry if CORE is not None else None
SITE = (14.3479, 100.5656, 12.0)  # the fake PWI4's site (conftest BASE)
SITE_HAE = GEO.Geodetic(SITE[0], SITE[1], SITE[2] + SITE_HAE_OFFSET_M) if GEO else None
T0 = 1_800_000_000.0
KT = 1852.0 / 3600.0
LIM_EL = 15.0
ICAO = "ABC123"


# ------------------------------------------------------------------ synthetic aircraft


class Flight:
    """Truth of one aircraft: horizontal motion from the site's east / north plane at a constant
    HAE height (+ vertical rate), a constant turn rate when `turn` is given (deg/s, clockwise)."""

    def __init__(
        self,
        e0: float,
        n0: float,
        h_above_site: float,
        speed: float,
        heading: float,
        *,
        turn: float = 0.0,
        vrate_fpm: float = 0.0,
        icao: str = ICAO,
        callsign: str | None = "TST100",
        t_ref: float = T0,
        site=None,
    ) -> None:
        self.site = site or SITE_HAE  # (the sim tests place the flight at PWI4's own site)
        self.e0, self.n0, self.h0 = e0, n0, self.site.h + h_above_site
        self.v, self.th0, self.turn = speed, heading, turn
        self.vrate = vrate_fpm
        self.icao, self.callsign, self.t_ref = icao, callsign, t_ref

    def heading(self, t: float) -> float:
        return self.th0 + self.turn * (t - self.t_ref)

    def enu(self, t: float) -> tuple[float, float]:
        dt = t - self.t_ref
        th0 = math.radians(self.th0)
        if abs(self.turn) < 1e-9:
            return self.e0 + self.v * math.sin(th0) * dt, self.n0 + self.v * math.cos(th0) * dt
        w = math.radians(self.turn)
        th = th0 + w * dt
        return (
            self.e0 + self.v / w * (math.cos(th0) - math.cos(th)),
            self.n0 + self.v / w * (math.sin(th) - math.sin(th0)),
        )

    def geodetic(self, t: float):
        e, n = self.enu(t)
        p = GEO.from_enu(GEO.Geodetic(self.site.lat, self.site.lon, 0.0), e, n, 0.0)
        return GEO.Geodetic(p.lat, p.lon, self.h0 + self.vrate * 0.3048 / 60.0 * (t - self.t_ref))

    def altaz(self, t: float, refract: bool = True) -> tuple[float, float]:
        lk = GEO.look_angles(self.site, self.geodetic(t))
        return (refract_alt(lk.el) if refract else lk.el), lk.az

    def row(self, t_fix: float, pos_age: float) -> dict:
        g = self.geodetic(t_fix)
        return fake_adsb.feed_row(
            self.icao, g.lat, g.lon, g.h, callsign=self.callsign, gs=self.v / KT,
            track=self.heading(t_fix) % 360.0, vrate=self.vrate,
            turn=self.turn if self.turn else None, pos_age=pos_age,
        )  # fmt: skip

    def exit_time(self, t_from: float, alt_min: float = LIM_EL) -> float:
        """First time after t_from the observed altitude is below alt_min."""
        t = t_from
        while self.altaz(t)[0] >= alt_min:
            t += 0.05
        return t


def record(flight: Flight, t_rx: float, latency: float = 0.3, prev=None, **row_kw):
    row = flight.row(t_rx - latency, latency)
    row.update(row_kw)
    return adapt_feed_aircraft(CORE, row, t_rx, prev)


class FakeRunner:
    """What the gateway reads from AdsbRunner: green / detail() / core / range_limit_m /
    aircraft_for()."""

    def __init__(self) -> None:
        self.core = CORE
        self.green = True
        self.text = "ADS-B feed down"
        self.range_limit_m = 100_000.0
        self.aircraft: dict[str, object] = {}

    def detail(self) -> str:
        return self.text

    def aircraft_for(self, icao: str):
        return self.aircraft.get(icao)


def sep(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Sky separation (deg) of two (alt, az) pairs."""
    return angular_sep_deg(a[1], a[0], b[1], b[0])


def checker(runner, *, sun=None, lim_el: float = LIM_EL, settings=None, now: float = T0):
    return AirChecker(
        lambda: runner, lambda: lim_el, lambda site, t: sun, lambda: now, lambda: settings
    )


STATUS = parse(status_text())


def evaluate(runner, icao: str = ICAO, *, status=STATUS, **kw):
    return checker(runner, **kw).evaluate(icao, SITE, status)


def runner_with(flight: Flight, **row_kw) -> FakeRunner:
    r = FakeRunner()
    r.aircraft[flight.icao] = record(flight, T0, **row_kw)
    return r


# the overflight used by most tests: GA, 3 km up, 2 km north of the site, 130 m/s due east, over
# the site's meridian at T0 + 60 s
GA = dict(e0=-130.0 * 60.0, n0=2000.0, h_above_site=3000.0, speed=130.0, heading=90.0)


def ga(**kw) -> Flight:
    return Flight(**{**GA, **kw})


# ------------------------------------------------------------------ command schema


def test_command_icao_schema():
    c = validate_command("x", "track_aircraft", {"icao": "abc123"})
    assert c.args == {"icao": "ABC123"}
    for bad in ("abc12", "abc1234", "xyz123", "", "ABC12 "):
        with pytest.raises(CommandError):
            validate_command("x", "track_aircraft", {"icao": bad})
    with pytest.raises(CommandError):
        validate_command("x", "track_aircraft", {"icao": "ABC123", "extra": 1})
    with pytest.raises(CommandError):
        validate_command("x", "track_aircraft", {})


# ------------------------------------------------------------------ the check rules (no app)


def test_rule_1_adsb_availability():
    f = ga()
    v = evaluate(None)
    assert (v.reason, v.code) == ("ADS-B not available", "adsb_down")
    r = runner_with(f)
    r.core = None
    assert evaluate(r).reason == "ADS-B not available"
    r = runner_with(f)
    r.green = False
    v = evaluate(r)
    assert (v.reason, v.code) == ("ADS-B feed down", "adsb_down")
    assert v.plan is None and v.aircraft is None


def test_rule_2_not_in_view():
    r = runner_with(ga())
    v = evaluate(r, "FFFFFF")
    assert (v.reason, v.code) == ("Aircraft not in view", "not_in_view")
    row = ga().row(T0 - 0.3, 0.3)
    row["lat"] = row["lon"] = row["pos_age_s"] = None  # no position
    r.aircraft["AAA111"] = adapt_feed_aircraft(CORE, {**row, "icao": "AAA111"}, T0)
    assert evaluate(r, "AAA111").code == "not_in_view"


def test_rule_3_stale_fix_and_target():
    r = FakeRunner()
    r.aircraft[ICAO] = record(ga(), T0, latency=3.5)
    v = evaluate(r)
    assert (v.reason, v.code) == ("ADS-B position too old", "stale_fix")
    assert v.target is not None  # the position is known: the card shows where it is
    r.aircraft[ICAO] = record(ga(), T0, latency=2.9)
    assert evaluate(r).reason is None  # inside the 3 s


def test_rule_4_out_of_range():
    r = runner_with(ga(e0=150_000.0, n0=0.0, speed=100.0, heading=270.0))
    v = evaluate(r)
    assert (v.reason, v.code) == ("Out of range", "out_of_range")
    r = runner_with(ga())
    r.range_limit_m = 5000.0
    assert evaluate(r).reason == "Out of range"


def test_rule_5_below_lim_el_and_crossing():
    # 40 km out, 1 km up, flying away: below the limit and never crossing
    away = Flight(40_000.0, 0.0, 1000.0, 200.0, 90.0)
    v = evaluate(runner_with(away))
    assert (v.reason, v.code) == ("Below Lim EL", "below_limit")
    # 14 km out at 3 km (12 deg), inbound at 200 m/s: crosses Lim EL + 0.5 within ~17 s
    f = Flight(14_100.0, 1500.0, 3000.0, 200.0, 270.0)
    assert f.altaz(T0)[0] < LIM_EL
    v = evaluate(runner_with(f))
    assert v.reason is None, v
    t = T0
    while f.altaz(t)[0] < LIM_EL + 0.5:
        t += 0.05
    assert v.plan.t_r >= t + 0.5 - 1e-6  # the rendezvous waits for the crossing + 0.5 s


def test_rule_6_sun():
    f = ga()
    alt, az = f.altaz(T0)
    v = evaluate(runner_with(f), sun=(alt + 14.0, az))
    assert (v.reason, v.code) == ("Within 15° of the Sun", "sun")
    assert evaluate(runner_with(f), sun=(alt + 25.0, az)).reason is None


def test_rule_7_zenith_rate_and_note():
    # straight over the site at 10.7 km, 250 m/s: the azimuth flips at the zenith
    over = Flight(-250.0 * 30, 0.0, 10_700.0, 250.0, 90.0)
    v = evaluate(runner_with(over))
    assert (v.reason, v.code) == ("Overflight too close to the zenith", "zenith")
    # a slow aircraft 300 m aside at 6 km: peak > 87 deg, needs ~11 deg/s < 0.8 * 20: allowed + note
    slow = Flight(-60.0 * 30, 300.0, 6000.0, 60.0, 90.0)
    v = evaluate(runner_with(slow))
    assert v.reason is None, v
    assert at.R_ZENITH_NOTE in v.notes
    assert v.aircraft["peak_el"] >= 87.0 and v.aircraft["t_peak"] == pytest.approx(T0 + 30, abs=2)
    # the same pass, with a slower mount axis 0 (vmax 8 deg/s -> limit 6.4): refused
    slow_mount = parse(status_text({"mount.axis0.max_velocity_degs_per_sec": 8}))
    v = evaluate(runner_with(slow), status=slow_mount)
    assert v.code == "zenith"
    # a normal fly-by has no note and reports its peak
    v = evaluate(runner_with(ga()))
    assert v.reason is None and v.notes == []
    assert v.aircraft["peak_el"] == pytest.approx(56.3, abs=0.6)
    assert v.aircraft["icao"] == ICAO and v.aircraft["callsign"] == "TST100"


def test_rule_8_rendezvous():
    # a mount that is very slow cannot reach the aircraft within 60 s
    slow = parse(status_text({"mount.axis1.max_velocity_degs_per_sec": 0.1}))
    v = evaluate(runner_with(ga()), status=slow)
    assert (v.reason, v.code) == ("Cannot reach the aircraft in time", "no_rendezvous")
    # the rendezvous aims at the PREDICTED position at t_r
    v = evaluate(runner_with(ga()))
    assert v.reason is None and v.plan is not None
    p = v.plan
    assert 3.0 <= p.t_r - T0 <= 60.0
    want = ga().altaz(p.t_r)
    assert sep((p.rendezvous.alt, p.rendezvous.az), want) < 0.02
    # an azimuth no axis-0 representation reaches (range -100..100, az ~240): no window fits
    narrow = parse(
        status_text(
            {"mount.axis0.min_mech_position_degs": -100, "mount.axis0.max_mech_position_degs": 100}
        )
    )
    f = Flight(-3000.0, -2500.0, 4000.0, 100.0, 0.0)
    v = evaluate(runner_with(f), status=narrow)
    assert (v.reason, v.code) == ("Track does not fit the wrap window", "no_rendezvous")


def test_rendezvous_picks_the_representation_that_keeps_the_track_in_its_window():
    # az 279.5 climbing toward north: the nearest representation (279.5, window [-60, 300]) is left
    # after 18 deg; -80.5 (window [-260.5, 99.5]) holds the whole track
    f = Flight(-3000.0, 500.0, 4000.0, 100.0, 0.0)
    v = evaluate(runner_with(f))
    assert v.reason is None, v
    assert v.plan.rendezvous.axis0 < 0.0
    assert v.plan.wrap_min == pytest.approx(at.window_for(v.plan.rendezvous.axis0, (-300.0, 300.0)))
    assert v.plan.rendezvous.axis0 % 360.0 == pytest.approx(v.plan.rendezvous.az % 360.0)


def test_wrap_window_is_centred_and_clamped():
    assert at.window_for(200.0, (-300.0, 300.0)) == -60.0  # 200 - 180 = 20 -> clamped to hi - 360
    assert at.window_for(-250.0, (-300.0, 300.0)) == -300.0  # -430 -> clamped to lo
    assert at.window_for(0.0, (-300.0, 300.0)) == -180.0  # centred, no clamp
    assert at.window_for(100.0, (-300.0, 300.0)) == -80.0
    assert at.window_for(150.0, (-100.0, 100.0)) == -30.0  # range < 360: no clamp
    # the nearest representation first
    assert at.axis0_candidates(120.0, 300.0, (-300.0, 300.0)) == [-60.0, 300.0]
    assert at.axis0_candidates(120.0, 120.0, (-300.0, 300.0))[0] == 120.0


def test_d1_note_when_settings_differs():
    f = ga()
    near = (SITE[0] + 0.0001, SITE[1], SITE[2])  # ~11 m
    assert evaluate(runner_with(f), settings=near).notes == []
    far = (SITE[0] + 0.001, SITE[1], SITE[2])  # ~110 m
    res = evaluate(runner_with(f), settings=far)
    assert res.target == evaluate(runner_with(f)).target  # PWI4's site, not Settings'
    notes = res.notes
    assert len(notes) == 1
    m = re.fullmatch(r"Settings location differs from PWI4's site by (\d+) m", notes[0])
    assert m and 100 <= int(m.group(1)) <= 125, notes


def test_d2_refraction_is_applied(monkeypatch):
    f = ga()
    v = evaluate(runner_with(f))
    geo = f.altaz(T0 - 0.0, refract=False)[0]
    assert v.target[0] == pytest.approx(refract_alt(geo), abs=2e-4)
    assert v.target[0] > geo + 0.01  # ~ 1.5' at 20 deg
    monkeypatch.setattr(at, "AIRCRAFT_REFRACT", False)
    v = evaluate(runner_with(f))
    assert v.target[0] == pytest.approx(geo, abs=2e-4)


def test_rule_order_first_failing_wins():
    f = ga(e0=150_000.0, n0=0.0, speed=100.0, heading=270.0)
    r = FakeRunner()
    r.aircraft[ICAO] = record(f, T0, latency=4.0)  # stale AND out of range
    assert evaluate(r).code == "stale_fix"
    r.aircraft[ICAO] = record(f, T0)  # out of range AND below Lim EL (it is at 150 km, 3 km up)
    assert evaluate(r).code == "out_of_range"


# ------------------------------------------------------------------ the path maths (no app)


def ctx_for(runner=None, *, sun=None, range_m=100_000.0, vmax0=20.0, mech=(-300.0, 300.0)):
    return at.AirCtx(CORE, SITE_HAE, LIM_EL, sun, range_m, vmax0, mech)


def test_path_points_spacing_and_truth():
    f = ga()
    ac = record(f, T0)
    path = at.build_path(ctx_for(), ac, T0 - 0.5, T0 + 10.0, -60.0)
    assert path.reason is None and len(path.points) == 22
    ts = [p.t for p in path.points]
    assert ts[0] == T0 - 0.5 and ts[-1] == pytest.approx(T0 + 10.0)
    assert all(b - a == pytest.approx(0.5) for a, b in zip(ts, ts[1:], strict=False))
    errs = [sep((p.alt, p.az), f.altaz(p.t)) for p in path.points]
    assert max(errs) < 0.02  # the fix is 0.3 s old: the points are in the present
    # axis 0 sits inside the window
    assert all(-60.0 <= p.axis0 < 300.0 for p in path.points)
    assert all(p.axis0 % 360.0 == pytest.approx(p.az % 360.0, abs=1e-9) for p in path.points)


def test_turning_flight_follows_predict_path():
    f = Flight(-2500.0, 2500.0, 3000.0, 60.0, 90.0, turn=3.0)
    ac = record(f, T0)
    ctx = ctx_for()
    path = at.build_path(ctx, ac, T0 - 0.5, T0 + 10.0, -180.0)
    assert path.reason is None
    # identical to predict.path -> look angles -> refraction
    for (t, p), pt in zip(
        CORE.predict.path(ac, T0 - 0.5, T0 + 10.0, 0.5), path.points, strict=True
    ):
        alt, az, _ = at.look(ctx, p)
        assert (pt.t, pt.alt, pt.az) == (t, alt, az)
    errs = [sep((p.alt, p.az), f.altaz(p.t)) for p in path.points]
    assert sum(errs) / len(errs) < 0.05 and max(errs) < 0.3
    # a straight-line prediction of the same aircraft is worse: the turn model matters
    straight = record(f, T0, turn_dps=None)
    sp = at.build_path(ctx, straight, T0 - 0.5, T0 + 10.0, -180.0)
    assert max(sep((p.alt, p.az), f.altaz(p.t)) for p in sp.points) > max(errs) * 2


def test_cut_lim_el_exit():
    f = ga()
    t_exit = f.exit_time(T0 + 60.0)
    ac = record(f, t_exit - 4.0, prev=None)
    ctx = ctx_for()
    path = at.build_path(ctx, ac, t_exit - 4.5, t_exit + 6.0, -60.0)
    assert path.reason == at.R_END_BELOW
    assert path.points[-1].alt >= LIM_EL and path.points[-1].t < t_exit
    assert path.points[-1].t >= t_exit - 0.5 - 1e-6  # cut at the first point below
    assert all(p.alt >= LIM_EL for p in path.points)


def test_cut_sun_16_degrees():
    f = ga()
    ac = record(f, T0 + 30.0)
    t_sun = T0 + 33.0
    alt, az = f.altaz(t_sun)
    sun = (alt + 15.9, az)  # 15.9 deg from the aircraft at t_sun: inside the 16 deg circle
    path = at.build_path(ctx_for(sun=sun), ac, T0 + 29.5, T0 + 40.0, -180.0)
    assert path.reason == at.R_END_SUN
    assert all(at.sun_sep(p.az, p.alt, sun) >= 16.0 for p in path.points)
    assert path.points[-1].t < t_sun + 0.5
    clear = at.build_path(ctx_for(sun=(alt + 17.0, az)), ac, T0 + 29.5, T0 + 31.5, -180.0)
    assert clear.reason is None


def test_cut_wrap_window_range_and_zenith():
    f = ga()
    ac = record(f, T0)
    # a window whose upper edge the azimuth reaches: ax0 climbs through the margin
    w = f.altaz(T0 + 5.0)[1] - 358.5  # hi = w + 358 sits just below the az at T0 + 5 s
    path = at.build_path(ctx_for(), ac, T0, T0 + 10.0, w)
    assert path.reason == at.R_END_WRAP and 0 < len(path.points) < 21
    # mechanical range
    narrow = at.build_path(ctx_for(mech=(-300.0, 287.0)), ac, T0, T0 + 10.0, -60.0)
    assert narrow.reason == at.R_END_WRAP and 0 < len(narrow.points) < 21
    # range limit (after the closest approach the aircraft recedes)
    late = record(f, T0 + 80.0)
    rng = GEO.look_angles(SITE_HAE, f.geodetic(T0 + 85.0)).range
    far = at.build_path(ctx_for(range_m=rng), late, T0 + 80.0, T0 + 90.0, -180.0)
    assert far.reason == at.R_END_RANGE and 0 < len(far.points) < 21
    # zenith rate: a fly-by at 3.7 deg/s against a mount axis 0 of vmax 4 (limit 3.2)
    slow = at.build_path(ctx_for(vmax0=4.0), record(f, T0 + 55.0), T0 + 54.5, T0 + 65.0, -180.0)
    assert slow.reason == at.R_END_ZENITH
    ok = at.build_path(ctx_for(vmax0=20.0), record(f, T0 + 55.0), T0 + 54.5, T0 + 65.0, -180.0)
    assert ok.reason is None


def test_no_prediction_basis_gives_reason():
    f = ga()
    ac = record(f, T0, gs_kt=None, track_deg=None)
    path = at.build_path(ctx_for(), ac, T0, T0 + 5.0, -60.0)
    assert path.points == [] and path.reason == at.R_PREDICTION


# ------------------------------------------------------------------ the rig (clock, feed, mount)


class Rig:
    """Fake air clock + sleep, a 1 Hz feed with 0.3 s latency, and the fake mount."""

    def __init__(
        self, app, fake: FakePwi4, flight: Flight, runner: FakeRunner, start=T0, monitor=False
    ):
        self.app, self.fake, self.flight, self.runner = app, fake, flight, runner
        if not monitor:  # the executor tests do not move the fake mount along the path
            app.state.monitor.tick = lambda: None
        self.control = app.state.control
        self.t = start
        self.latency = 0.3
        self.rx_next = start + 1.0  # the first fix is primed below, a fix arrives each second
        self.feed_stop_at: float | None = None  # no fix is delivered after this time
        self.no_velocity = False
        self.last_fix_t = start - self.latency
        self.park_delay: dict[int, float] = {}  # goto number -> fake seconds before it "arrives"
        self.goto_seen: dict[int, float] = {}
        self.parked = 0
        self.follow = False  # move the fake mount along the uploaded path
        self.real_s = 0.01
        self.offset = (0.0, 0.0)  # the fake mount's deviation from the path (alt, axis0), deg
        self.upload_times: list[float] = []
        self.sleeps: list[float] = []
        self.seen: list[tuple[str | None, tuple | None]] = []  # (phase, state override) per wait
        self.pause_at: float | None = None
        self.paused = threading.Event()
        self._gate: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.control.adsb_runner = runner
        self.control.air_clock = lambda: self.t
        self.control.air_sleep = self.sleep
        runner.aircraft[flight.icao] = record(flight, start, self.latency)

    # -- feed
    def _feed(self, rx: float) -> None:
        row = self.flight.row(rx - self.latency, self.latency)
        if self.no_velocity:
            row["gs_kt"] = row["track_deg"] = row["turn_dps"] = None
        prev = self.runner.aircraft.get(self.flight.icao)
        self.runner.aircraft[self.flight.icao] = adapt_feed_aircraft(CORE, row, rx, prev)
        self.last_fix_t = rx - self.latency

    def _advance(self, dt: float) -> None:
        target = self.t + dt
        while self.rx_next <= target + 1e-9:
            if self.feed_stop_at is not None and self.rx_next > self.feed_stop_at:
                self.rx_next = math.inf
                break
            self.t = max(self.t, self.rx_next)
            self._feed(self.rx_next)
            self.rx_next += 1.0
        self.t = target

    # -- mount
    def _follow(self) -> None:
        a = self.control.airx.a
        ups = uploads(self.fake)
        if not self.follow or a is None or not ups or a.phase == "slewing":
            return
        pts = [((jd - 2440587.5) * 86400.0, az, alt) for jd, az, alt in ups[-1]]
        t = min(max(self.t, pts[0][0]), pts[-1][0])
        for (t0, az0, al0), (t1, az1, al1) in zip(pts, pts[1:], strict=False):
            if t0 <= t <= t1:
                u = (t - t0) / (t1 - t0)
                az = az0 + ((az1 - az0 + 180.0) % 360.0 - 180.0) * u
                alt = al0 + (al1 - al0) * u
                ax0 = a.wrap_min + (az - a.wrap_min) % 360.0
                park_fake(self.fake, ax0 + self.offset[1], alt + self.offset[0])
                return

    async def sleep(self, dt: float) -> None:
        self._loop = asyncio.get_running_loop()
        a = self.control.airx.a
        n_up = sum(1 for p, _ in self.fake.writes if p.endswith("add_point_list"))
        self.upload_times += [self.t] * (n_up - len(self.upload_times))
        self.sleeps.append(self.t)
        self.seen.append((a.phase if a else None, state.get_override()))
        real = self.real_s
        if a is not None and a.phase == "slewing":
            gotos = sum(1 for p, _ in self.fake.writes if p == "/mount/goto_alt_az")
            if gotos > self.parked:
                self.goto_seen.setdefault(gotos, self.t)
                if self.t >= self.goto_seen[gotos] + self.park_delay.get(gotos, 0.0):
                    rdv = a.plan.rendezvous
                    park_fake(self.fake, rdv.axis0, rdv.alt)
                    self.parked = gotos
            real = 0.15 if self.parked else real  # the poller needs a few cycles to see the pose
        if self.pause_at is not None and self.t + dt >= self.pause_at:
            part = max(0.0, self.pause_at - self.t)
            self._advance(part)
            dt -= part
            self.pause_at = None
            self._gate = asyncio.Event()
            self.paused.set()
            await self._gate.wait()
        self._advance(dt)
        self._follow()
        await asyncio.sleep(real)

    def wait_paused(self, timeout: float = 20.0) -> bool:
        ok = self.paused.wait(timeout)
        self.paused.clear()
        return ok

    def resume(self) -> None:
        if self._loop is not None and self._gate is not None:
            self._loop.call_soon_threadsafe(self._gate.set)


def plan_now(app, icao: str = ICAO):
    ctl = app.state.control
    res = ctl.air_eval(icao, ctl.poller.pwi4_site(), ctl.fresh_status())
    assert res.reason is None, res.reason
    return res.plan


def start_track(tc, app, flight=None, *, pause_after_tr: float | None = 20.0, runner=None, **rigkw):
    """A running app, a rig, the command sent at T0; returns (rig, win, plan, ack)."""
    fake = rigkw.pop("fake")
    flight = flight or ga()
    runner = runner or FakeRunner()
    rig = Rig(app, fake, flight, runner, **rigkw)
    plan = plan_now(app)
    if pause_after_tr is not None:
        rig.pause_at = plan.t_r + pause_after_tr
    w = Win(tc)
    ack = w.send("track_aircraft", {"icao": flight.icao.lower()}, "k1")
    return rig, w, plan, ack


def fake_end(tc, pred, timeout=30.0):
    wait_for(pred, timeout)


def run_to_end(app, rig, timeout=40.0):
    wait_for(lambda: not app.state.control.airx.active, timeout)


def ended(tc):
    s = snap_when(tc, lambda s: s["airtrack"] is not None and s["airtrack"]["ended"] is not None)
    return s["airtrack"]["ended"] if s["airtrack"] else None


# ------------------------------------------------------------------ the check endpoint


def test_check_endpoint_body_and_parity(tmp_path):
    with running() as (tc, fake, app):
        rig = Rig(app, fake, ga(), FakeRunner())
        d = check(tc, "track_aircraft", {"icao": "abc123"})
        assert set(d) >= {"ok", "reason", "code", "target", "notes", "aircraft", "detour"}
        assert d["ok"] is True and d["reason"] is None and d["code"] is None
        assert set(d["target"]) == {"alt", "az"}
        assert d["aircraft"]["icao"] == ICAO and d["aircraft"]["callsign"] == "TST100"
        assert d["aircraft"]["peak_el"] > 50 and d["notes"] == []
        assert fake.writes == []  # a dry run never writes
        # unknown aircraft: refused, with the same reason from the real command
        d = check(tc, "track_aircraft", {"icao": "FFFFFF"})
        assert (d["ok"], d["reason"], d["code"]) == (False, "Aircraft not in view", "not_in_view")
        assert d["target"] is None and d["aircraft"] is None
        w = Win(tc)
        ack = w.send("track_aircraft", {"icao": "FFFFFF"}, "r1")
        assert ack["ok"] is False and ack["reason"] == "Aircraft not in view"
        # not green
        rig.runner.green = False
        d = check(tc, "track_aircraft", {"icao": ICAO})
        assert (d["reason"], d["code"]) == ("ADS-B feed down", "adsb_down")
        assert w.send("track_aircraft", {"icao": ICAO}, "r2")["reason"] == "ADS-B feed down"
        # no runner at all: the gateway works, the aircraft feature is refused with a reason
        app.state.control.adsb_runner = None
        d = check(tc, "track_aircraft", {"icao": ICAO})
        assert (d["reason"], d["code"]) == ("ADS-B not available", "adsb_down")
        # row 0 (the mount guards) wins: a Go To in flight... here an unknown field is a 422
        r = tc.post(
            "/api/mount/check",
            json={"cmd": "track_aircraft", "args": {"icao": "nothex"}},
            headers={"x-mount-token": "t" * 43, "origin": "http://127.0.0.1:3200"},
        )
        assert r.status_code == 422
        assert fake.writes == []
        w.close()
        assert not [e for e in audit_lines(tmp_path) if e["event"] == "track_aircraft"]


def test_check_row_0_wins_over_aircraft_rules():
    fake = FakePwi4()
    fake.set(mount__axis0__is_enabled=False)
    with running(fake) as (tc, fake, app):
        Rig(app, fake, ga(), FakeRunner())
        d = check(tc, "track_aircraft", {"icao": ICAO})
        assert d["reason"] == "Axis 0 disabled" and d["code"] == "axis_disabled"
        assert d["aircraft"] is None


def test_gateway_without_adsb_module_keeps_working():
    """No adsb_core: the gateway starts, track_aircraft is refused, a Go To still works."""
    from fastapi.testclient import TestClient

    from gateway.app import create_app
    from gateway.config import Settings

    from .test_control import HDR, TOKEN

    fake = FakePwi4()
    app = create_app(
        Settings(port=8400, token=TOKEN),
        transport=fake.transport,
        adsb_loader=lambda: (None, "ADS-B module not installed"),
    )
    with TestClient(app, base_url="http://127.0.0.1:8400") as tc:
        wait_for(lambda: app.state.control.ready)
        wait_for(lambda: app.state.control.fresh_status() is not None)
        wait_for(lambda: app.state.control.homed.blocking)
        app.state.control.homed.override("test-fixture")
        assert app.state.adsb.core is None
        d = check(tc, "track_aircraft", {"icao": ICAO})
        assert (d["ok"], d["reason"], d["code"]) == (False, "ADS-B not available", "adsb_down")
        s = tc.get("/api/mount/status").json()
        assert s["airtrack"] is None
        with tc.websocket_connect("/api/mount/ws", headers=HDR) as ws:
            ws.send_text(json.dumps({"type": "auth", "token": TOKEN}))
            ws.receive_text()
            ws.send_text(
                json.dumps({"type": "cmd", "id": "g1", "cmd": "goto_altaz",
                            "args": {"alt": 50.0, "az": 130.0}})
            )  # fmt: skip
            ack = None
            while ack is None:
                msg = json.loads(ws.receive_text())
                ack = msg if msg["type"] == "ack" else None
            assert ack["ok"] is True


# ------------------------------------------------------------------ the whole overflight


def test_straight_overflight_sequence_updates_and_end(tmp_path):
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig, w, plan, ack = start_track(tc, app, fake=fake)
        assert ack["ok"] is True and ack["cmd"] == "track_aircraft", ack
        assert rig.wait_paused(), (rig.t, rig.sleeps[-3:])
        a = ctl.airx.a
        assert a is not None and a.phase == "tracking"
        assert a.t_r == pytest.approx(plan.t_r)  # the command ran the check's verdict

        # --- the PWI4 call sequence: the window ONCE before the goto, then new / add / apply(0)
        names = paths(fake)
        goto = names.index("/mount/goto_alt_az")
        wraps = [i for i, p in enumerate(names) if p == "/mount/set_axis0_wrap_range_min"]
        assert wraps == [wraps[0]] and wraps[0] < goto
        calls = dict(enumerate(fake.writes))
        assert float(calls[wraps[0]][1]["degs"]) == pytest.approx(plan.wrap_min, abs=1e-6)
        g = calls[goto][1]
        assert float(g["alt_degs"]) == pytest.approx(plan.rendezvous.alt, abs=1e-6)
        assert float(g["az_degs"]) == pytest.approx(plan.rendezvous.az % 360.0, abs=1e-6)
        # the wrap window is centred on the rendezvous axis 0 (clamped inside +-300)
        assert plan.wrap_min == pytest.approx(
            at.window_for(plan.rendezvous.axis0, (-300.0, 300.0)), abs=1e-6
        )
        n_new = names.count("/mount/custom_path/new")
        n_add = names.count("/mount/custom_path/add_point_list")
        n_apply = names.count("/mount/custom_path/apply")
        assert n_new == n_add == n_apply >= 20
        assert all(
            p == {"update_wrap": "0"} for q, p in fake.writes if q == "/mount/custom_path/apply"
        )
        assert names.count("/mount/set_axis0_wrap_range_min") == 1  # never moved afterwards

        # --- one update per second; the first point is t_r (waiting) / now - 0.5 (tracking)
        ups = uploads(fake)
        assert len(ups) == len(rig.upload_times) == n_add
        gaps = [b - a_ for a_, b in zip(rig.upload_times, rig.upload_times[1:], strict=False)]
        assert all(g_ == pytest.approx(1.0, abs=1e-5) for g_ in gaps), gaps
        waiting = [i for i, u in enumerate(rig.upload_times) if u < plan.t_r]
        assert waiting and waiting[-1] < len(ups) - 5
        for i, (u, pts) in enumerate(zip(rig.upload_times, ups, strict=True)):
            ts = [(jd - 2440587.5) * 86400.0 for jd, _, _ in pts]
            if u < plan.t_r:
                assert ts[0] == pytest.approx(plan.t_r, abs=1e-3), (i, u)
            else:
                assert ts[0] == pytest.approx(u - 0.5, abs=1e-3), (i, u)
                assert ts[-1] <= u + 10.0 + 1e-3
                assert len(pts) >= 20
            assert all(
                (b - a_) == pytest.approx(0.5, abs=1e-3) for a_, b in zip(ts, ts[1:], strict=False)
            )
            assert all(0.0 <= az < 360.0 for _, az, _ in pts)
        # the points are the truth in the present (latency compensation), refracted (D2)
        err = []
        for pts in ups:
            for jd, az, alt in pts:
                err.append(sep((alt, az), rig.flight.altaz((jd - 2440587.5) * 86400.0)))
        assert max(err) < 0.02, max(err)

        # --- phases and the state override, the snapshot
        phases = []
        for ph, _ in rig.seen:
            if not phases or phases[-1] != ph:
                phases.append(ph)
        assert phases[:3] == ["slewing", "waiting", "tracking"], phases
        assert ("waiting", ("TRACKING", "Waiting for TST100")) in rig.seen
        assert ("tracking", ("TRACKING", "Tracking TST100")) in rig.seen
        s = snap_when(tc, lambda s: s["airtrack"] and s["airtrack"]["phase"] == "tracking")
        at_ = s["airtrack"]
        assert set(at_) == {"icao", "callsign", "phase", "t_r", "notes", "ended"}
        assert (at_["icao"], at_["callsign"], at_["phase"]) == (ICAO, "TST100", "tracking")
        assert at_["t_r"] == pytest.approx(plan.t_r) and at_["notes"] == [] and at_["ended"] is None
        assert s["state"] == "TRACKING" and s["state_reason"] == "Tracking TST100"
        assert s["track"] is None  # the satellite field is untouched (D4)

        # --- run to the end: Lim EL exit, ended 1 s after the last kept point
        n_before = len(ups)
        rig.resume()
        run_to_end(app, rig)
        t_exit = rig.flight.exit_time(T0 + 60.0)
        e = ended(tc)
        assert e["icao"] == ICAO and e["reason"] == "Aircraft below Lim EL"
        assert t_exit + 0.4 <= e["at"] <= t_exit + 2.1, e["at"] - t_exit
        tail = paths(fake)[names.index("/mount/custom_path/apply") :]
        assert "/mount/stop" in tail and tail[-1] != "/mount/custom_path/apply"
        assert len(uploads(fake)) > n_before
        wait_for(lambda: state_is_stopped("Aircraft below Lim EL"), 5.0)
        s = snap_when(tc, lambda s: s["airtrack"] and s["airtrack"]["phase"] is None)
        assert s["state"] == "STOPPED" and s["state_reason"] == "Aircraft below Lim EL"
        assert s["airtrack"]["icao"] is None and s["airtrack"]["ended"]["icao"] == ICAO
        assert app.state.monitor.plan is None

        # --- audit: ICAO + callsign only, never positions
        start = events(tmp_path, "track_aircraft")
        assert len(start) == 1 and start[0]["icao"] == ICAO and start[0]["callsign"] == "TST100"
        assert set(start[0]) <= {"ts", "event", "client", "id", "icao", "callsign", "t_r"}
        done = events(tmp_path, "airtrack_done")
        assert len(done) == 1 and done[0]["reason"] == "Aircraft below Lim EL"
        assert set(done[0]) <= {
            "ts", "event", "client", "id", "icao", "callsign", "reason", "phase", "elapsed_s",
        }  # fmt: skip
        blob = json.dumps([x for x in audit_lines(tmp_path) if "air" in x["event"]])
        for bad in ("lat", "lon", "alt", "token", "position"):
            assert bad not in blob.replace("callsign", "")

        # --- ended lasts 30 s; the next command clears the STOPPED reason
        assert ctl.airx.info() is not None
        ack = w.send("tracking", {"on": False}, "k2")
        assert ack["ok"] and state_cleared()
        rig.t += 31.0
        assert ctl.airx.info() is None
        w.close()


def state_is_stopped(reason: str) -> bool:
    from gateway import state

    return state.get_override() == ("STOPPED", reason)


def state_cleared() -> bool:
    from gateway import state

    return state.get_override() is None


def test_callsign_falls_back_to_icao_and_state_text():
    with running() as (tc, fake, app):
        f = ga(callsign=None)
        rig, w, plan, ack = start_track(tc, app, f, fake=fake, pause_after_tr=10.0)
        assert ack["ok"], ack
        assert rig.wait_paused()
        s = snap_when(tc, lambda s: s["state"] == "TRACKING")
        assert s["state_reason"] == f"Tracking {ICAO}" and s["airtrack"]["callsign"] is None
        w.send("stop", None, "s1")
        w.close()


def test_turning_flight_is_followed():
    with running() as (tc, fake, app):
        f = Flight(-2600.0, 2400.0, 2500.0, 60.0, 90.0, turn=3.0)
        rig, w, plan, ack = start_track(tc, app, f, fake=fake, pause_after_tr=25.0)
        assert ack["ok"], ack
        assert rig.wait_paused()
        errs = []
        for u, pts in zip(rig.upload_times, uploads(fake), strict=True):
            if u < plan.t_r + 3.0:  # (3 s settle, as the sim spec)
                continue
            for jd, az, alt in pts:
                errs.append(sep((alt, az), f.altaz((jd - 2440587.5) * 86400.0)))
        assert errs and sum(errs) / len(errs) < 0.05 and max(errs) < 0.3, (max(errs),)
        w.send("stop", None, "s1")
        w.close()


# ------------------------------------------------------------------ feed gaps (Q60)


def test_fix_9_s_old_continues_and_11_s_stops():
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=None)
        assert ack["ok"], ack
        rig.feed_stop_at = T0 + 40.5  # the last fix arrives at T0 + 40 (taken at T0 + 39.7)
        rig.pause_at = T0 + 39.7 + 9.2  # its age is 9.2 s
        assert rig.wait_paused()
        assert ctl.airx.active and ctl.airx.a.phase == "tracking"
        last = rig.last_fix_t
        assert rig.t - last == pytest.approx(9.2, abs=0.05)
        rig.resume()
        run_to_end(app, rig)
        e = ended(tc)
        assert e["reason"] == "ADS-B data lost"
        assert 10.0 < e["at"] - last <= 11.0 + 1e-6, e["at"] - last
        names = paths(fake)
        assert "/mount/stop" in names[-6:] or "/mount/tracking_off" in names[-3:]
        wait_for(lambda: state_is_stopped("ADS-B data lost"), 5.0)
        w.close()


def test_kalman_fallback_for_6_s_stops():
    with running() as (tc, fake, app):
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=None)
        assert ack["ok"], ack
        t_switch = plan.t_r + 8.0
        rig.pause_at = t_switch
        assert rig.wait_paused()
        rig.no_velocity = True  # the records lose their velocity: the Kalman filter predicts
        rig.resume()
        run_to_end(app, rig)
        e = ended(tc)
        assert e["reason"] == "ADS-B prediction unreliable"
        assert 5.0 <= e["at"] - t_switch <= 7.5, e["at"] - t_switch
        w.close()


def test_feed_not_green_stops_with_its_detail():
    with running() as (tc, fake, app):
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=10.0)
        assert ack["ok"] and rig.wait_paused()
        t = rig.t
        rig.runner.green = False
        rig.runner.text = "ADS-B platform unreachable"
        rig.resume()
        run_to_end(app, rig)
        e = ended(tc)
        assert e["reason"] == "ADS-B platform unreachable" and 0.0 <= e["at"] - t <= 1.0 + 1e-6
        assert "/mount/stop" in paths(fake)
        w.close()


def test_aircraft_disappears_is_lost():
    with running() as (tc, fake, app):
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=10.0)
        assert ack["ok"] and rig.wait_paused()
        rig.feed_stop_at = -1.0
        rig.runner.aircraft.clear()
        rig.resume()
        run_to_end(app, rig)
        assert ended(tc)["reason"] == "Aircraft lost"
        w.close()


# ------------------------------------------------------------------ cuts during the track


def test_sun_approaching_cuts_the_path(tmp_path):
    with running() as (tc, fake, app):
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=6.0)
        assert ack["ok"] and rig.wait_paused()
        t_sun = rig.t + 5.0
        alt, az = rig.flight.altaz(t_sun)
        app.state.safety.sun_override = (alt + 15.0, az)  # inside 16 deg from t_sun on
        t_in = rig.t
        while sep(rig.flight.altaz(t_in), (alt + 15.0, az)) >= 16.0:
            t_in += 0.05
        rig.resume()
        run_to_end(app, rig)
        e = ended(tc)
        assert e["reason"] == "Sun approaching"
        assert t_in - 0.5 + 0.9 <= e["at"] <= t_in + 2.1, e["at"] - t_in
        assert events(tmp_path, "airtrack_done")[0]["reason"] == "Sun approaching"
        assert "/mount/stop" in paths(fake)
        w.close()


def test_rate_cut_ends_with_zenith_reason():
    with running() as (tc, fake, app):
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=30.0)
        assert ack["ok"] and rig.wait_paused()
        # the closest approach (3.7 deg/s) is ~10 s ahead; the mount's axis 0 is "slowed" to 4 deg/s
        fake.set(mount__axis0__max_velocity_degs_per_sec=4)
        rig.resume()
        run_to_end(app, rig)
        e = ended(tc)
        assert e["reason"] == "Overflight too close to the zenith"
        t_peak = T0 + 60.0
        assert rig.t < t_peak + 5.0  # ended around the closest approach, not after the exit
        w.close()


def test_out_of_range_cut():
    with running() as (tc, fake, app):
        f = Flight(8_000.0, 2_000.0, 3000.0, 130.0, 90.0)  # east-bound, leaving
        rig, w, plan, ack = start_track(tc, app, f, fake=fake, pause_after_tr=3.0)
        assert ack["ok"], ack
        assert rig.wait_paused()
        rig.runner.range_limit_m = GEO.look_angles(SITE_HAE, f.geodetic(rig.t + 6.0)).range
        rig.resume()
        run_to_end(app, rig)
        assert ended(tc)["reason"] == "Out of range"
        w.close()


def test_wrap_window_cut():
    with running() as (tc, fake, app):
        # circling the site clockwise at 3 km radius: the azimuth turns 2.5 deg/s from 0 deg on;
        # the window [-180, 180] (centred on the rendezvous axis 0) ends at ax0 = 178
        f = Flight(0.0, 3000.0, 3000.0, 130.0, 90.0, turn=math.degrees(130.0 / 3000.0))
        rig, w, plan, ack = start_track(tc, app, f, fake=fake, pause_after_tr=None)
        assert ack["ok"], ack
        assert plan.wrap_min == pytest.approx(plan.rendezvous.axis0 - 180.0)  # centred (no clamp)
        run_to_end(app, rig, 60.0)
        e = ended(tc)
        assert e["reason"] == "Track left the wrap window"
        t_edge = T0
        while at.axis0_in(rig.flight.altaz(t_edge)[1], plan.wrap_min) < plan.wrap_min + 358.0:
            t_edge += 0.05
        assert t_edge - 0.5 <= e["at"] <= t_edge + 1.1  # (<2 points left: ends at once)
        # the window was set once, before the goto
        names = paths(fake)
        assert names.count("/mount/set_axis0_wrap_range_min") == 1
        w.close()


# ------------------------------------------------------------------ late arrival


def test_late_arrival_replans_once_then_gives_up(tmp_path):
    with running() as (tc, fake, app):
        ctl = app.state.control
        runner = FakeRunner()
        rig = Rig(app, fake, ga(), runner)
        plan = plan_now(app)
        rig.park_delay = {1: 60.0}  # the first goto "arrives" far too late
        w = Win(tc)
        ack = w.send("track_aircraft", {"icao": ICAO}, "k1")
        assert ack["ok"], ack
        wait_for(lambda: ctl.airx.a is not None and ctl.airx.a.replanned or not ctl.airx.active, 15)
        # a second goto went out for the new rendezvous, the mount arrived: it tracks
        wait_for(lambda: ctl.airx.active and ctl.airx.a.phase in ("waiting", "tracking"), 15)
        assert paths(fake).count("/mount/goto_alt_az") == 2
        assert events(tmp_path, "airtrack_replan") and ctl.airx.a.t_r > plan.t_r
        w.send("stop", None, "s1")
        w.close()
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig = Rig(app, fake, ga(), FakeRunner())
        rig.park_delay = {1: 60.0, 2: 60.0}  # late twice
        w = Win(tc)
        assert w.send("track_aircraft", {"icao": ICAO}, "k1")["ok"]
        wait_for(lambda: not ctl.airx.active, 20)
        assert ended(tc)["reason"] == "Missed the aircraft"
        assert "/mount/stop" in paths(fake)
        w.close()


# ------------------------------------------------------------------ G5: replacement


def test_goto_stop_and_link_loss_end_the_aircraft_track(tmp_path):
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=8.0)
        assert ack["ok"] and rig.wait_paused()
        # Go To replaces it ("Replaced by goto_altaz"), no `ended`, the mount is stopped first
        ack = w.send("goto_altaz", {"alt": 60.0, "az": 100.0}, "g1")
        assert ack["ok"], ack
        assert not ctl.airx.active
        ev = events(tmp_path, "airtrack_replaced")
        assert ev and ev[0]["reason"] == "Replaced by goto_altaz"
        assert ctl.airx.info() is None
        names = paths(fake)
        assert names.index("/mount/stop") < len(names) - 1
        w.close()
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=8.0)
        assert ack["ok"] and rig.wait_paused()
        assert w.send("stop", None, "s1")["ok"]
        assert not ctl.airx.active
        e = ended(tc)
        assert e["reason"] == "Stopped by STOP"
        assert events(tmp_path, "airtrack_stopped")[-1]["reason"] == "Stopped by STOP"
        w.close()
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=8.0)
        assert ack["ok"] and rig.wait_paused()
        w.close()  # the holder's window goes away
        wait_for(lambda: not ctl.airx.active, 10)
        assert ctl.airx.ended is not None and ctl.airx.ended.reason == "Control link lost"


def start_satellite(tc, w, sat: SatRig, cid: str) -> None:
    """track_object on the satellite rig, held 20 fake seconds after its rendezvous (the rig's
    clock would otherwise run the whole pass in a blink)."""
    d = check(tc, "track_object", {"norad": 99001})
    assert d["ok"], d
    sat.pause_at = d["pass"]["t_r"] + 20.0
    ack = w.send("track_object", {"norad": 99001}, cid)
    assert ack["ok"], ack
    assert sat.wait_paused(), sat.log[-3:]
    snap_when(tc, lambda s: s["state"] == "TRACKING")  # the poller republishes at 10 Hz


def test_satellite_and_aircraft_tracks_replace_each_other(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        ctl = app.state.control
        # aircraft -> satellite
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=8.0)
        assert ack["ok"] and rig.wait_paused()
        tle_row(stub)
        sat = SatRig(app, fake, stub)
        start_satellite(tc, w, sat, "t1")
        assert not ctl.airx.active and ctl.trackx.active
        ev = events(tmp_path, "airtrack_replaced")
        assert ev and ev[-1]["reason"] == "Replaced by a new track"
        assert ctl.airx.info() is None and snap_when(tc, lambda s: s["airtrack"] is None)
        # satellite -> aircraft
        assert w.send("stop", None, "s1")["ok"]
        wait_for(lambda: not ctl.trackx.active, 5)
        snap_when(tc, lambda s: s["state"] != "SLEWING")
        start_satellite(tc, w, sat, "t2")
        assert ctl.trackx.active
        ack = w.send("track_aircraft", {"icao": ICAO}, "k2")
        assert ack["ok"], ack
        assert ctl.airx.active and not ctl.trackx.active
        rep = events(tmp_path, "track_replaced")
        assert rep and rep[-1]["reason"] == "Replaced by a new track"
        # a new aircraft track ends the old one
        ack = w.send("track_aircraft", {"icao": ICAO}, "k3")
        assert ack["ok"], ack
        assert len(events(tmp_path, "airtrack_replaced")) >= 2
        assert ctl.airx.active
        w.send("stop", None, "s2")
        w.close()


# ------------------------------------------------------------------ the safety monitor


def test_replacements_do_not_trip_the_corridor_monitor(tmp_path):
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig = Rig(app, fake, ga(), FakeRunner(), monitor=True)
        rig.follow = True
        rig.real_s = 0.12  # the poller and the 10 Hz monitor see every fake second
        plan = plan_now(app)
        rig.pause_at = plan.t_r + 30.0
        w = Win(tc)
        assert w.send("track_aircraft", {"icao": ICAO}, "k1")["ok"]
        assert rig.wait_paused(60.0)
        assert ctl.airx.active and ctl.airx.a.phase == "tracking"
        mon = app.state.monitor
        assert mon.plan is not None and mon.plan.route and mon.plan.sky  # replaced, not restarted
        assert mon.stops == 0
        assert not events(tmp_path, "limit_stop")
        assert len(uploads(fake)) >= 30
        # a positive control: the mount 6 deg off the path trips the corridor
        rig.offset = (6.0, 0.0)
        rig.resume()
        wait_for(lambda: not ctl.airx.active, 15)
        assert mon.stops >= 1
        stopped = events(tmp_path, "airtrack_stopped")
        assert stopped and stopped[-1]["reason"] == "Mount left the validated slew path"
        w.close()


def test_monitor_replace_route_keeps_its_clock_and_plan():
    from gateway.safety import SafetyMonitor

    class _Svc:
        def fresh_status(self):
            return None

    mon = SafetyMonitor.__new__(SafetyMonitor)
    mon.plan = None
    assert mon.replace_route([(0.0, 20.0)], (0.0, 20.0), 5.0) is False
    SafetyMonitor.begin_route(
        mon, [(0.0, 20.0), (1.0, 20.0)], (1.0, 20.0), 8.0, None, (0.0, 20.0), True
    )
    t_begin = mon.plan.t_begin
    assert mon.replace_route([(5.0, 30.0), (6.0, 30.0)], (6.0, 30.0), 4.0) is True
    p = mon.plan
    assert p.t_begin == t_begin and p.route and p.sky
    assert p.paths == [[(5.0, 30.0), (6.0, 30.0)]] and p.target == (6.0, 30.0)
    assert 4.0 <= p.duration_s < 5.0


# ------------------------------------------------------------------ the mount is never left moving


def test_a_crashed_loop_stops_the_mount(monkeypatch):
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=6.0)
        assert ack["ok"] and rig.wait_paused()
        n_stops = paths(fake).count("/mount/stop")

        def boom(*a, **k):
            raise RuntimeError("synthetic bug")

        monkeypatch.setattr(at, "build_path", boom)
        rig.resume()
        run_to_end(app, rig)
        assert ctl.airx.ended.reason == "Track failed (RuntimeError)"
        wait_for(lambda: paths(fake).count("/mount/stop") > n_stops, 5.0)
        assert paths(fake)[-1] in ("/mount/tracking_off", "/mount/stop")
        w.close()


def test_a_failed_upload_stops_the_mount():
    with running() as (tc, fake, app):
        ctl = app.state.control
        rig, w, plan, ack = start_track(tc, app, fake=fake, pause_after_tr=6.0)
        assert ack["ok"], ack
        assert rig.wait_paused()
        fake.fail_writes.add("/mount/custom_path/apply")  # PWI4 refuses the next replacement
        rig.resume()
        run_to_end(app, rig)
        assert "PWI4 refused the request" in ctl.airx.ended.reason
        assert "/mount/stop" in paths(fake)
        w.close()
