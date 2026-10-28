"""T3.1b: safety engine. Pure-function tests, predictor vs recorded sim slews, forged commands
(I4: refused with NO PWI4 write), and the predictive 10 Hz monitor against FakePwi4.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import pytest

from gateway.safety import (
    AxisParams,
    SafetyMonitor,
    ValidatedGoto,
    axis0_target,
    corridor_distance,
    monitor_decision,
    parse_sun_override,
    path_min_sun_sep,
    predict_paths,
    r_below,
    simulate_axis,
    sun_sep,
    sun_shortcut,
    validated_decision,
)

from .conftest import FakePwi4
from .test_control import Win, audit_lines, running, up_radec, wait_for

FIXTURE = Path(__file__).parent / "fixtures" / "sim_slews.json"
LIM = r_below(15.0)


# ------------------------------------------------------------------ pure functions


def test_sun_shortcut_boundaries():
    # whole disc below Lim EL and below the mount's own altitude
    assert sun_shortcut(-40, 15, 45)
    assert sun_shortcut(-0.01, 15, 45)  # -0.01 + 15 < 15
    assert not sun_shortcut(0.0, 15, 45)  # disc top touches Lim EL
    assert not sun_shortcut(10, 15, 45)
    # recovery move: mount itself below Lim EL -> the mount altitude term governs
    assert not sun_shortcut(-10, 15, 3)  # -10 + 15 = 5 is not < 3
    assert sun_shortcut(-20, 15, 3)


def test_axis0_target_nearest_and_range():
    assert axis0_target(120, 100, -300, 300) == 100
    assert axis0_target(-250, 100, -300, 300) == -260
    assert axis0_target(213.7, 333.7, -300, 300) == pytest.approx(-26.3)  # 333.7 out of range
    assert axis0_target(0, 200, 0, 100) is None
    assert axis0_target(10, 10, -300, 300) == 10


def test_parse_sun_override():
    assert parse_sun_override("12.5,-30") == (12.5, -30.0)
    assert parse_sun_override("") is None and parse_sun_override("x,y") is None
    assert parse_sun_override(None) is None


def test_simulate_axis_shapes():
    p = simulate_axis(0, 90, 20, 1e6, 0.5)
    assert p[0] == 0 and p[-1] == 90
    assert all(b >= a - 1e-9 for a, b in zip(p, p[1:], strict=False))  # monotonic, no overshoot
    assert max(p) <= 90 + 1e-9
    slow = simulate_axis(0, 90, 20, 20, 0.5)
    assert slow[3] < p[3]  # the acceleration limit lags the start of the slew
    assert simulate_axis(10, 10, 20, 20, 0.5) == [10, 10]


def test_predict_paths_variants_end_at_target():
    paths = predict_paths((10, 20), (100, 60), AxisParams(20, 20), AxisParams(20, 20))
    assert len(paths) == 2
    for p in paths:
        assert p[0] == (10, 20) and p[-1] == (100, 60)


def test_path_min_sun_sep_finds_crossing_and_start():
    paths = predict_paths((100, 45), (200, 45), AxisParams(20, 20), AxisParams(20, 20))
    low, start = path_min_sun_sep(paths, (45.0, 150.0))
    assert low < 2.0  # passes straight through
    assert start == pytest.approx(sun_sep(100, 45, (45, 150)))
    low2, _ = path_min_sun_sep(paths, (-30.0, 0.0))
    assert low2 > 60


# ------------------------------------------------------------------ predictor vs recorded sim slews


def _prediction_errors(accel: float, latency: float = 0.02) -> list[float]:
    d = json.loads(FIXTURE.read_text())
    runs, samples = d["runs"], d["samples"]
    errs: list[float] = []
    for k, r in enumerate(runs):
        t1 = runs[k + 1]["t_cmd"] - 0.05 if k + 1 < len(runs) else r["t_cmd"] + 30
        seg = [s for s in samples if r["t_cmd"] - 0.1 <= s[0] < t1]
        for ax in (0, 1):
            x0, x1 = r["start"][ax], r["target"][ax]
            if abs(x1 - x0) < 1e-6:
                continue
            tr = simulate_axis(x0, x1, 20, accel, 0.5, sample_dt=0.005, dt=0.005)
            for s in seg:
                tt = s[0] - r["t_cmd"] - latency
                i = max(0, min(len(tr) - 1, round(tt / 0.005)))
                pred = x0 if tt < 0 else tr[i]
                errs.append(abs(pred - s[1 + ax]))
    return errs


def test_predictor_matches_recorded_sim_slews():
    """12 recorded sim slews (alt/az, 5-180 deg): the no-acceleration-limit variant tracks the
    sim to < 1 deg max / 0.3 deg rms; the reported-accel variant is the slower bound."""
    fast = _prediction_errors(1e6)
    rms = math.sqrt(sum(e * e for e in fast) / len(fast))
    assert max(fast) < 1.0 and rms < 0.3, (max(fast), rms)
    slow = _prediction_errors(20.0)
    assert max(slow) > 5.0  # documents that the nominal-accel variant alone would be off


# ------------------------------------------------------------------ monitor decision


def test_monitor_decision_limit_el():
    d = monitor_decision(17.0, 100, -5.0, 0, 15.0, None)  # 17 - 2.5 = 14.5 < 15
    assert d and d[0] == LIM
    assert monitor_decision(18.0, 100, -5.0, 0, 15.0, None) is None  # 15.5 >= 15
    assert monitor_decision(30.0, 100, 0.0, 0, 15.0, None) is None  # stationary
    assert monitor_decision(30.0, 100, +9.0, 0, 15.0, None) is None  # ascending


def test_monitor_decision_recovery_inside_limit():
    # already below Lim EL: moving up / not descending is allowed, descending is stopped
    assert monitor_decision(10.0, 100, +3.0, 0, 15.0, None) is None
    assert monitor_decision(10.0, 100, 0.0, 0, 15.0, None) is None
    d = monitor_decision(10.0, 100, -1.0, 0, 15.0, None)
    assert d and d[0] == LIM


def test_monitor_decision_sun_closing_only():
    sun = (45.0, 150.0)
    # at az 120, closing at +30 deg/s: predicted az 135 -> ~10 deg from the Sun
    d = monitor_decision(45.0, 120.0, 0.0, 30.0, 15.0, sun)
    assert d and d[0] == "Within 15° of the Sun"
    assert monitor_decision(45.0, 120.0, 0.0, -30.0, 15.0, sun) is None  # moving away
    assert monitor_decision(45.0, 120.0, 0.0, 5.0, 15.0, sun) is None  # predicted 122.5 still far
    # inside the disc but moving away: recovery
    assert monitor_decision(45.0, 148.0, 0.0, -10.0, 15.0, sun) is None
    # inside the disc and closing deeper: stop
    assert monitor_decision(45.0, 140.0, 0.0, 10.0, 15.0, sun) is not None
    # shortcut -> sun=None -> no Sun check
    assert monitor_decision(45.0, 120.0, 0.0, 30.0, 15.0, None) is None


# ------------------------------------------------------------------ I4: forged commands


def forged(tc_name: str, args: dict, *, sun: str = "-60,0", reason: str, fake=None, mp=None):
    """Send one command to a running gateway; assert the exact refusal and NO PWI4 write."""
    mp.setenv("PW_TEST_SUN_ALTAZ", sun)
    with running(fake) as (tc, fk, _):
        w = Win(tc)
        ack = w.send(tc_name, args)
        assert ack["ok"] is False and ack["reason"] == reason, ack
        assert fk.writes == [], fk.writes
        w.close()


def test_i4_goto_altaz_below_lim_el(monkeypatch):
    forged("goto_altaz", {"alt": 10, "az": 100}, reason=LIM, mp=monkeypatch)
    forged("goto_altaz", {"alt": 14.99, "az": 100}, reason=LIM, mp=monkeypatch)


def test_i4_goto_radec_below_horizon_p14(monkeypatch):
    ra, dec = up_radec()
    # 12 h away on the same meridian circle: far below the horizon
    forged(
        "goto_radec_j2000",
        {"ra_h": (ra + 12.0) % 24.0, "dec_deg": -dec},
        reason=LIM,
        mp=monkeypatch,
    )


def test_i4_target_inside_sun_disc(monkeypatch):
    # fake mount points alt 45 / az 120; Sun at alt 45 / az 125 (5 deg away)
    forged(
        "goto_altaz", {"alt": 45, "az": 130}, sun="45,125", reason="Within 15° of the Sun",
        mp=monkeypatch,
    )  # fmt: skip


def test_i4_path_crossing_sun_without_detour(monkeypatch):
    # T3.5c: a crossing path is detoured; here the Sun sits where neither side is usable
    # (Lim EL below, a 40 deg axis-1 limit above), so it stays a refusal, with no PWI4 write
    fake = FakePwi4()
    fake.set(
        mount__altitude_degs=30.0, mount__axis1__position_degs=30.0,
        mount__axis1__max_mech_position_degs=40.0, mount__azimuth_degs=130.0,
        mount__axis0__position_degs=130.0,
    )  # fmt: skip
    forged(
        "goto_altaz", {"alt": 30, "az": 230}, sun="30,180", fake=fake,
        reason="Slew path passes within 15° of the Sun; no safe detour", mp=monkeypatch,
    )  # fmt: skip


def test_i4_tracking_on_refusals(monkeypatch):
    fake = FakePwi4()
    fake.set(mount__altitude_degs=10.0, mount__axis1__position_degs=10.0)
    forged("tracking", {"on": True}, reason=LIM, fake=fake, mp=monkeypatch)
    forged("tracking", {"on": True}, sun="45,125", reason="Within 15° of the Sun", mp=monkeypatch)


def test_i4_site_mismatch_refuses_motion_not_stop(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("take_control")["ok"]
        fake.set(site__latitude_degs=20.0)  # PWI4's site now differs from Settings
        wait_for(lambda: (app.state.poller.current_site_check() or object()).ok is False)
        for name, args in (
            ("goto_altaz", {"alt": 40, "az": 125}),
            ("goto_radec_j2000", {"ra_h": 1, "dec_deg": 5}),
            ("tracking", {"on": True}),
        ):
            ack = w.send(name, args, name)
            assert ack["reason"] == "Station location differs from PWI4's site", ack
        assert fake.writes == []
        assert w.send("stop", cid="s")["ok"]  # STOP never site-gated
        assert w.send("release_control", cid="r")["ok"]
        assert w.send("take_control", cid="t")["ok"]
        assert w.send("tracking", {"on": False}, "off")["ok"]  # tracking off is not motion
        w.close()


def test_refusals_are_audited(monkeypatch, tmp_path):
    forged("goto_altaz", {"alt": 5, "az": 100}, reason=LIM, mp=monkeypatch)
    ev = audit_lines(tmp_path)
    assert any(e["event"] == "safety_refusal" and e["reason"] == LIM for e in ev)
    assert any(e["event"] == "cmd" and e["result"] == "refused" and e["reason"] == LIM for e in ev)


def test_sun_shortcut_skips_sun_checks(monkeypatch):
    # Sun disc (alt -40) far below Lim EL: a target 'inside' it is not a Sun problem
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-40,125")
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 45, "az": 130})["ok"]
        w.close()
    # Sun at alt -0.5: disc top is above Lim EL - 15 + ... shortcut holds (-0.5 + 15 < 15)
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-0.5,125")
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 15, "az": 125})["ok"]
        w.close()
    # Sun at alt 0: disc top touches Lim EL -> NOT hidden; alt 15 is exactly 15 deg away
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "0,125")
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 15, "az": 125})["reason"] == "Within 15° of the Sun"
        assert fake.writes == []
        w.close()


def test_recovery_start_inside_sun_disc(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,125")  # mount at az 120 alt 45: 3.5 deg away
    with running() as (tc, fake, _):
        w = Win(tc)
        # away from the Sun and never closer: allowed
        assert w.send("goto_altaz", {"alt": 45, "az": 60}, "a")["ok"]
        # through / toward the Sun: refused
        ack = w.send("goto_altaz", {"alt": 45, "az": 200}, "b")
        assert ack["reason"] == "Slew path passes within 15° of the Sun"
        w.close()


def test_recovery_start_below_lim_el(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    fake.set(mount__altitude_degs=10.0, mount__axis1__position_degs=10.0)
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 40, "az": 125}, "up")["ok"]  # back up: allowed
        assert w.send("goto_altaz", {"alt": 12, "az": 125}, "down")["reason"] == LIM
        assert w.send("tracking", {"on": True}, "trk")["reason"] == LIM
        w.close()


def test_radec_goto_sets_wrap_from_resolved_azimuth():
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        w = Win(tc)
        ra, dec = up_radec()  # meridian, south of zenith: az ~180
        assert w.send("goto_radec_j2000", {"ra_h": ra, "dec_deg": dec})["ok"]
        paths = [p for p, _ in fake.writes]
        assert paths == ["/mount/set_axis0_wrap_range_min", "/mount/goto_ra_dec_j2000"]
        w.close()


# ------------------------------------------------------------------ monitor (FakePwi4, 10 Hz)


def _drive(fake: FakePwi4, alt0: float, rate: float, az: float, stop_evt: threading.Event, log):
    """Move the fake mount's altitude at `rate` deg/s until STOP arrives."""
    t0 = time.monotonic()
    while not stop_evt.is_set():
        if any(p == "/mount/stop" for p, _ in fake.writes):
            return
        alt = alt0 + rate * (time.monotonic() - t0)
        fake.set(
            mount__altitude_degs=round(alt, 4),
            mount__axis1__position_degs=round(alt, 4),
            mount__is_slewing=True,
        )
        log.append(alt)
        time.sleep(0.02)


def test_monitor_stops_descent_before_lim_el(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    evt, log = threading.Event(), []
    with running(fake) as (_, _, app):
        th = threading.Thread(target=_drive, args=(fake, 30.0, -4.0, 120, evt, log))
        th.start()
        wait_for(lambda: any(p == "/mount/stop" for p, _ in fake.writes), timeout=12)
        evt.set()
        th.join(2)
        alt_at_stop = log[-1]
        assert app.state.monitor.stops == 1
    assert alt_at_stop >= 15.0, alt_at_stop  # stopped before reaching Lim EL
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "limit_stop"]
    assert len(ev) == 1 and ev[0]["reason"] == LIM and ev[0]["delivered"] is True


def test_monitor_ignores_recovery_and_stationary(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    fake.set(mount__altitude_degs=10.0, mount__axis1__position_degs=10.0)
    evt, log = threading.Event(), []
    with running(fake) as (_, _, app):
        time.sleep(1.0)  # stationary inside the limit
        assert app.state.monitor.stops == 0
        th = threading.Thread(target=_drive, args=(fake, 10.0, +3.0, 120, evt, log))
        th.start()
        time.sleep(2.0)  # ascending out of the limit: recovery
        evt.set()
        th.join(2)
        assert app.state.monitor.stops == 0
        assert fake.writes == []


def test_monitor_skips_sun_check_under_shortcut(monkeypatch):
    # Sun far below Lim EL: a mount "heading into" its direction is not stopped
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-40,150")
    fake = FakePwi4()
    with running(fake) as (_, _, app):
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            az = 120 + 14 * (time.monotonic() - t0)
            fake.set(mount__azimuth_degs=round(az, 3), mount__axis0__position_degs=round(az, 3))
            time.sleep(0.02)
        assert app.state.monitor.stops == 0


def test_monitor_stops_when_closing_on_sun(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,160")
    fake = FakePwi4()
    with running(fake) as (_, _, app):
        t0 = time.monotonic()
        while time.monotonic() - t0 < 6.0 and not any(p == "/mount/stop" for p, _ in fake.writes):
            az = 120 + 10 * (time.monotonic() - t0)
            fake.set(
                mount__azimuth_degs=round(az, 3),
                mount__axis0__position_degs=round(az, 3),
                mount__is_slewing=True,
            )
            time.sleep(0.02)
        assert any(p == "/mount/stop" for p, _ in fake.writes)
        assert app.state.monitor.stops == 1
        sep = sun_sep(az, 45, (45, 160))
    assert sep > 15.0  # stopped before the disc
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "limit_stop"]
    assert ev and ev[0]["reason"] == "Within 15° of the Sun"


# ---- validated gotos (no extrapolation) ----

P20 = AxisParams(20, 20)


def _plan(start, target, sun_sep_start=None) -> ValidatedGoto:
    paths = predict_paths(start, target, P20, P20, 0.5)
    return ValidatedGoto(
        start=start,
        target=target,
        paths=paths,
        duration_s=max(len(p) for p in paths) * 0.1,
        start_sun_sep=sun_sep_start,
    )


@pytest.mark.parametrize("margin", [0.5, 0.1])
def test_validated_descending_slew_to_lim_el_plus_margin_is_not_stopped(margin):
    """Regression (supervisor gate): 85 -> Lim EL + margin at ~20 deg/s. Linear extrapolation
    (0.5 s look-ahead = 10 deg) cut such legal slews short; the validated rule must not."""
    plan = _plan((100.0, 85.0), (100.0, 15.0 + margin))
    stops_linear = 0
    for path in plan.paths:
        for a0, a1 in path:
            assert validated_decision(a1, (a0, a1), plan, 15.0, None, a0) is None
    # the old rule, fed the monitor's real 0.2 s-window velocity, fires on the same samples
    pth = plan.paths[1]
    for i in range(2, len(pth)):
        v = (pth[i][1] - pth[i - 2][1]) / 0.2
        if monitor_decision(pth[i][1], 100.0, v, 0.0, 15.0, None):
            stops_linear += 1
    assert stops_linear > 0


def test_validated_slew_passing_17deg_from_sun_is_not_stopped():
    sun = (68.0, 100.0)  # diagonal slew (100,30) -> (200,60) passes 17.0 deg from it
    plan = _plan((100.0, 30.0), (200.0, 60.0), sun_sep_start=sun_sep(100, 30, sun))
    low, _ = path_min_sun_sep(plan.paths, sun)
    assert 16.5 < low < 17.5  # the validated path really stays ~17 deg away
    for path in plan.paths:
        for a0, a1 in path:
            assert validated_decision(a1, (a0, a1), plan, 15.0, sun, a0) is None
    # the linear look-ahead rule, fed the monitor's 0.2 s-window velocity, WOULD have stopped it
    p = plan.paths[1]
    fired = False
    for i in range(2, len(p)):
        v0, v1 = (p[i][0] - p[i - 2][0]) / 0.2, (p[i][1] - p[i - 2][1]) / 0.2
        if monitor_decision(p[i][1], p[i][0] % 360, v1, v0, 15.0, sun):
            fired = True
    assert fired


def test_validated_slew_stops_on_actual_violation():
    plan = _plan((100.0, 85.0), (100.0, 15.0))
    below = validated_decision(14.9, (100.0, 14.9), plan, 15.0, None, 100.0)
    assert below and below[0] == LIM  # actual alt below Lim EL - 0.05
    assert validated_decision(14.96, (100.0, 14.96), plan, 15.0, None, 100.0) is None  # 0.04 under
    sun = (45.0, 150.0)
    plan2 = _plan((100.0, 45.0), (101.0, 45.0), sun_sep_start=sun_sep(100, 45, sun))
    d = validated_decision(45.0, (140.0, 45.0), plan2, 15.0, sun, 140.0)  # actually 10 deg away
    assert d and d[0] == "Within 15° of the Sun"


def test_validated_slew_stops_when_leaving_corridor():
    plan = _plan((100.0, 45.0), (160.0, 45.0))
    on_path = plan.paths[1][3]
    assert validated_decision(45.0, on_path, plan, 15.0, None, on_path[0]) is None
    # 1.5 deg off the path: inside the 2 deg corridor; 5 deg off: stop
    assert validated_decision(46.5, (on_path[0], 46.5), plan, 15.0, None, on_path[0]) is None
    d = validated_decision(50.0, (on_path[0], 50.0), plan, 15.0, None, on_path[0])
    assert d and d[0] == "Mount left the validated slew path"
    # wrong direction entirely
    d = validated_decision(45.0, (60.0, 45.0), plan, 15.0, None, 60.0)
    assert d and d[0] == "Mount left the validated slew path"


def test_validated_recovery_start_below_lim_el():
    plan = _plan((100.0, 10.0), (100.0, 40.0))
    assert validated_decision(10.0, (100.0, 10.0), plan, 15.0, None, 100.0) is None
    assert validated_decision(9.96, (100.0, 9.96), plan, 15.0, None, 100.0) is None
    d = validated_decision(9.9, (100.0, 9.9), plan, 15.0, None, 100.0)  # deeper than the start
    assert d and d[0] == LIM


def test_corridor_tolerance_vs_recorded_slews():
    """Justifies CORRIDOR_TOL_DEG: recorded sim slews stay within 0.1 deg of the predicted
    path (both variants); the 2 deg tolerance leaves ~50x margin."""
    d = json.loads(FIXTURE.read_text())
    runs, samples = d["runs"], d["samples"]
    worst = 0.0
    for k, r in enumerate(runs):
        t1 = runs[k + 1]["t_cmd"] - 0.05 if k + 1 < len(runs) else r["t_cmd"] + 30
        paths = predict_paths(tuple(r["start"]), tuple(r["target"]), P20, P20, 0.5)
        for s in samples:
            if r["t_cmd"] - 0.1 <= s[0] < t1:
                worst = max(worst, corridor_distance((s[1], s[2]), paths))
    assert worst < 0.1, worst
    assert corridor_distance((0.0, 0.0), [[(0.0, 0.0)]]) == 0.0


# ------------------------------------------------------------------ P15 + monitor with a real goto


def test_p15_goto_altaz_ends_tracking_first(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        fake.set(mount__is_tracking=True)
        time.sleep(0.3)
        w = Win(tc)
        n = len(fake.writes)
        assert w.send("goto_altaz", {"alt": 40, "az": 125})["ok"]
        paths = [p for p, _ in fake.writes][n:]
        assert paths[0] == "/mount/tracking_off" and paths[-1] == "/mount/goto_alt_az"
        assert fake.text.count("mount.is_tracking=false") == 1  # PWI4 model: tracking ended
        w.close()


def _follow(fake, plan_paths, evt, log, az=100.0, wrong=None):
    """Drive the fake mount along a predicted path in wall-clock time (instant-accel variant)."""
    path = plan_paths[1]
    t0 = time.monotonic()
    while not evt.is_set():
        if any(p == "/mount/stop" for p, _ in fake.writes):
            return
        i = int((time.monotonic() - t0) / 0.1)
        i = min(i, len(path) - 1)
        a0, a1 = path[i]
        if wrong is not None:
            a0, a1 = wrong(a0, a1)
        fake.set(
            mount__altitude_degs=round(a1, 4),
            mount__axis1__position_degs=round(a1, 4),
            mount__azimuth_degs=round(a0 % 360, 4),
            mount__axis0__position_degs=round(a0, 4),
            mount__is_slewing=i < len(path) - 1,
        )
        log.append((a0, a1))
        if i >= len(path) - 1:
            return
        time.sleep(0.02)


def _goto_scenario(monkeypatch, tmp_path, target_alt, *, wrong=None, validated=True):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    fake.set(
        mount__altitude_degs=85.0,
        mount__axis1__position_degs=85.0,
        mount__azimuth_degs=100.0,
        mount__axis0__position_degs=100.0,
    )
    with running(fake) as (tc, _, app):
        mon = app.state.monitor
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": target_alt, "az": 100.0})["ok"] or not validated
        plan = mon.plan
        assert plan is not None
        evt, log = threading.Event(), []
        th = threading.Thread(target=_follow, args=(fake, plan.paths, evt, log, 100.0, wrong))
        th.start()
        th.join(15)
        time.sleep(0.5)
        evt.set()
        stopped = any(p == "/mount/stop" for p, _ in fake.writes)
        stops = mon.stops
        w.close()
    return stopped, stops, log, audit_lines(tmp_path)


@pytest.mark.parametrize("margin", [0.5, 0.1])
def test_monitor_lets_validated_goto_reach_lim_el_plus_margin(monkeypatch, tmp_path, margin):
    stopped, stops, log, audit = _goto_scenario(monkeypatch, tmp_path, 15.0 + margin)
    assert not stopped and stops == 0, [e for e in audit if e["event"] == "limit_stop"]
    assert abs(log[-1][1] - (15.0 + margin)) < 0.05
    assert not [e for e in audit if e["event"] == "limit_stop"]


def test_monitor_stops_validated_goto_that_leaves_corridor(monkeypatch, tmp_path):
    # the mount heads 6 deg off in azimuth halfway through
    def wrong(a0, a1):
        return (a0 + 6.0, a1) if a1 < 60 else (a0, a1)

    stopped, stops, log, audit = _goto_scenario(monkeypatch, tmp_path, 20.0, wrong=wrong)
    assert stopped and stops == 1
    ev = [e for e in audit if e["event"] == "limit_stop"]
    assert ev and ev[0]["reason"] == "Mount left the validated slew path"


def test_plan_ends_on_stop_and_unvalidated_rules_resume(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    with running() as (tc, fake, app):
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 40, "az": 125})["ok"]
        assert app.state.monitor.plan is not None
        assert w.send("stop", cid="s")["ok"]
        assert app.state.monitor.plan is None
        assert w.send("tracking", {"on": True}, "t")["ok"]
        assert app.state.monitor.plan is None
        w.close()


def test_monitor_velocity_uses_the_azimuth_axis_not_the_flipping_azimuth():
    """Phase 3 end: when axis 1 passes the zenith the REPORTED azimuth flips by 180 deg while the
    azimuth axis barely moves. The monitor's v_az (and so its speed-dependent look-ahead) must
    follow the axis, or a descent from alt 87 is predicted below Lim EL."""
    mon = SafetyMonitor(None, None, None)  # type: ignore[arg-type]  # velocity() needs no I/O
    mon._hist.append((0.0, 89.5, 10.0, 10.0, 0.0))
    mon._hist.append((0.3, 87.7, 190.0, 10.3, 0.0))  # reported az flipped, axis 0 +0.3 deg
    v_alt, v_az = mon.velocity()
    assert abs(v_az - 1.0) < 1e-9 and abs(v_alt + 6.0) < 1e-9
    assert monitor_decision(87.7, 190.0, v_alt, v_az, 15.0, None) is None
    # no axis-0 position reported: the wrapped azimuth difference (previous behaviour)
    mon._hist.clear()
    mon._hist.append((0.0, 45.0, 359.0, math.nan, 0.0))
    mon._hist.append((0.5, 45.0, 1.0, math.nan, 0.0))
    assert abs(mon.velocity()[1] - 4.0) < 1e-9
