"""T4.2b: the object-track planner (pure). Fixed times near the test_passes epoch, synthetic
TLEs (test_passes / tlegen), never the wall clock.

Passes used (site and epoch of test_passes, Lim EL 15, seconds after T0):
  LEO   42945..43195  peak 28 deg, az 257 -> 2 (crosses north: the 0/360 wrap)
  LEO   78495..78805  peak 65 deg, az 314 -> 153 (the main pass)
  ZENITH_89           60..380 peak 89.0 deg
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from skyfield.api import EarthSatellite

from gateway.dsun import project, unproject
from gateway.passes import R_TOO_SHORT, PassInfo, _altaz
from gateway.safety import AxisParams
from gateway.trackplan import (
    CHUNK_S,
    R_NO_AZ_RANGE,
    R_NO_DETOUR,
    R_ZENITH_NOTE,
    TrackMount,
    TrackPlan,
    TrackRefusal,
    TrackSetup,
    _arc,
    _Sun,
    extend_track,
    plan_track,
    slew_estimate_s,
)
from gateway.transforms import refract_alt

from .test_passes import GEO, LEO, LIM, MEO, SITE, T0, TOPOS, TS, info_at
from .tlegen import PEAK_70, ZENITH_89

NIGHT = (-60.0, 0.0)
M = TrackMount(250.0, 40.0, AxisParams(20.0, 20.0), AxisParams(20.0, 20.0), (-300.0, 300.0), 90.0)
MAIN_NOW = T0 + 78500  # just inside the 65 deg pass
WRAP_NOW = T0 + 42950  # just inside the 28 deg pass whose azimuth crosses 0/360


def setup(tle, sun=None, mount=M, lim=LIM):
    fn = sun if sun is not None else (lambda t: NIGHT)
    return TrackSetup(tle[0], tle[1], SITE, lim, TS, fn, mount)


def planned(tle, now, sun=None, mount=M, lim=LIM, info=None):
    info = info if info is not None else info_at(tle, now, lim)
    res = plan_track(setup(tle, sun, mount, lim), info, now)
    return res, info


def ok(tle, now, **kw) -> TrackPlan:
    res, _ = planned(tle, now, **kw)
    assert isinstance(res, TrackPlan), res
    return res


def sep(az, alt, sun) -> float:
    from gateway.ephem import angular_sep_deg

    return angular_sep_deg(az, alt, sun[1], sun[0])


def sun_beside(plan: TrackPlan, frac: float, offset: float, left: bool = True):
    """A Sun position (alt, az) `offset` deg to the side of the track at `frac` of the plan."""
    i = int(frac * (len(plan.points) - 1))
    a, b = plan.points[i], plan.points[i + 1]
    x, y = project(b.az, b.alt, (a.alt, a.az))
    n = math.hypot(x, y)
    px, py = (-y / n, x / n) if left else (y / n, -x / n)
    az, alt = unproject(offset * px, offset * py, (a.alt, a.az))
    return (alt, az)


def bearing(az, alt, sun) -> float:
    return math.atan2(*project(az, alt, sun))


# ------------------------------------------------------------------ track points


def test_points_match_skyfield_with_pwi4_refraction():
    p = ok(LEO, MAIN_NOW)
    sat = EarthSatellite(*LEO, ts=TS)
    t = np.array([q.t for q in p.points])
    alt, az = _altaz(sat, TOPOS, TS, t)
    ref_alt = np.array([refract_alt(float(a)) for a in alt])
    assert max(abs(q.alt - r) for q, r in zip(p.points, ref_alt, strict=True)) <= 0.001
    d_az = np.abs(((np.array([q.az for q in p.points]) - az) + 180.0) % 360.0 - 180.0)
    assert d_az.max() <= 0.001
    # whole seconds, 1 s apart, the last point exactly at the Lim EL exit
    assert all(q.t == round(q.t) for q in p.points[:-1])
    assert np.allclose(np.diff(t[:-1]), 1.0)
    assert p.points[-1].t == pytest.approx(info_at(LEO, MAIN_NOW).t_out, abs=1e-6)
    assert p.t_end == p.points[-1].t and p.final is True and p.detours == []
    assert p.min_sun_sep_deg > 80
    assert p.corridor == [(q.axis0, q.alt) for q in p.points]


def test_axis0_is_continuous_across_north():
    p = ok(LEO, WRAP_NOW)
    az = np.array([q.az for q in p.points])
    assert az.max() > 355 and az.min() < 5 and (az[0] > 250 and az[-1] < 10)
    x = np.array([q.axis0 for q in p.points])
    assert np.abs(np.diff(x)).max() < 3.0
    assert np.allclose((x - az + 180.0) % 360.0 - 180.0, 0.0, atol=1e-9)
    assert -300.0 <= x.min() and x.max() <= 300.0  # shifted into the mount's range
    # the representation nearest the mount (axis 0 at 250) that fits: 257..362 does not, so -103..2
    assert x[0] == pytest.approx(az[0] - 360.0, abs=1e-6)


def test_axis0_continuous_through_a_near_zenith_flip():
    p = ok(ZENITH_89, T0 + 70)
    az = np.array([q.az for q in p.points])
    x = np.array([q.axis0 for q in p.points])
    step = np.abs(np.diff(x))
    assert step.max() < 190.0  # the flip is ~180 deg in a step or two, never 360
    assert np.abs(x[-1] - x[0]) < 300.0
    assert np.allclose((x - az + 180.0) % 360.0 - 180.0, 0.0, atol=1e-9)
    # one sense of rotation along the whole pass
    d = np.diff(x)
    assert (d >= -1e-9).all() or (d <= 1e-9).all()


def test_axis0_range_too_narrow_is_refused():
    narrow = replace(M, axis0_range=(-10.0, 10.0))
    res, _ = planned(LEO, MAIN_NOW, mount=narrow)
    assert isinstance(res, TrackRefusal) and (res.reason, res.code) == (R_NO_AZ_RANGE, "no_az")


def test_zenith_note_only_above_87_deg():
    assert ok(ZENITH_89, T0 + 70).notes == [R_ZENITH_NOTE]
    assert R_ZENITH_NOTE == "Near-zenith pass: tracking lags near the peak"
    assert ok(LEO, MAIN_NOW).notes == []
    info = info_at(LEO, MAIN_NOW)
    assert ok(LEO, MAIN_NOW, info=replace(info, peak_alt=87.0)).notes == []
    assert ok(LEO, MAIN_NOW, info=replace(info, peak_alt=87.01)).notes == [R_ZENITH_NOTE]


# ------------------------------------------------------------------ rendezvous


def test_rendezvous_track_phase_waits_for_the_slew():
    near = replace(M, axis0=-48.0, axis1=17.0)  # next to the object: a short slew
    p_near = ok(LEO, MAIN_NOW, mount=near)
    est_near = slew_estimate_s(near, p_near.rendezvous.az, p_near.rendezvous.alt)
    assert est_near < 4.0
    assert est_near + 3.0 - 1.0 <= p_near.t_r - MAIN_NOW <= est_near + 3.0 + 3.0
    far = replace(M, axis0=100.0, axis1=80.0)
    p_far = ok(LEO, MAIN_NOW, mount=far)
    est = slew_estimate_s(far, p_far.rendezvous.az, p_far.rendezvous.alt)
    assert est > 8.0
    assert est + 3.0 - 2.0 <= p_far.t_r - MAIN_NOW <= est + 3.0 + 3.0  # 3 iterations converge
    assert p_far.rendezvous is p_far.points[0] and p_far.t_r == p_far.points[0].t
    assert p_far.rendezvous.alt >= LIM + 0.5
    assert p_far.points[0].t == round(p_far.points[0].t)


def test_rendezvous_pre_position_is_the_lim_el_crossing_plus_pad():
    info = info_at(LEO, MAIN_NOW)
    now = info.t_in - 120.0  # 2 min before the object rises through Lim EL
    p = ok(LEO, now, mount=replace(M, axis0=-48.0, axis1=20.0))
    assert p.t_r >= info.t_in
    assert p.rendezvous.alt >= LIM + 0.5
    prev_alt = refract_alt(
        float(_altaz(EarthSatellite(*LEO, ts=TS), TOPOS, TS, np.array([p.t_r - 1]))[0][0])
    )
    assert (
        prev_alt < LIM + 0.5 + 0.3
    )  # the first second the pad is met (the climb is ~0.2-0.4 deg/s)
    # the slew takes longer than the wait: t_r = now + slew + 3 instead
    slow = replace(M, ax0=AxisParams(2.0, 5.0), axis0=-48.0 + 90.0)
    now2 = info.t_in - 10.0
    p2 = ok(LEO, now2, mount=slow)
    est = slew_estimate_s(slow, p2.rendezvous.az, p2.rendezvous.alt)
    assert est > 30.0 and p2.t_r > p.t_r
    assert p2.t_r - now2 == pytest.approx(est + 3.0, abs=3.0)


def test_pass_too_short_rules():
    info = info_at(LEO, MAIN_NOW)
    res = plan_track(setup(LEO), info, info.t_out - 20.0)  # row 6 itself
    assert (res.reason, res.code) == (R_TOO_SHORT, "pass_too_short")
    # 60 s left: fine with the mount on the object ...
    near = replace(M, axis0=-48.0, axis1=20.0)
    now = info.t_out - 60.0
    on = replace(near, axis0=ok(LEO, now).rendezvous.axis0, axis1=ok(LEO, now).rendezvous.alt)
    p = plan_track(setup(LEO, mount=on), info, now)
    assert isinstance(p, TrackPlan) and info.t_out - p.t_r >= 30.0
    # ... but a slow slew eats it: the rendezvous would leave < 30 s
    slow = replace(near, ax0=AxisParams(1.0, 1.0))
    res = plan_track(setup(LEO, mount=slow), info, now)
    assert (res.reason, res.code) == (R_TOO_SHORT, "pass_too_short")


def test_never_reaching_lim_el_plus_pad_is_peak_below():
    info = info_at(LEO, MAIN_NOW)  # peak 65.3 deg
    res = plan_track(
        setup(LEO, lim=65.0),
        replace(info, t_in=info.t_peak - 20.0, t_out=info.t_peak + 20.0),
        info.t_peak - 20.0,
    )
    assert isinstance(res, TrackRefusal) and res.code == "peak_below_limit"


# ------------------------------------------------------------------ D-SUN


def moving(sun, t_ref):
    return lambda t: (sun[0] + 0.004 * (t - t_ref), sun[1] + 0.003 * (t - t_ref))


def test_detour_clearance_all_along_the_arc_with_a_moving_sun():
    ref = ok(LEO, MAIN_NOW)
    sun0 = sun_beside(ref, 0.5, 3.0)
    t_ref = ref.points[len(ref.points) // 2].t
    fn = moving(sun0, t_ref)
    p = ok(LEO, MAIN_NOW, sun=fn)
    assert len(p.detours) == 1
    d = p.detours[0]
    assert d.side in ("cw", "ccw") and d.t_b > d.t_a + 5.0 and d.length_deg > 20.0
    arc = [q for q in p.points if d.t_a - 1e-9 <= q.t <= d.t_b + 1e-9]
    assert len(arc) >= (d.t_b - d.t_a) / 0.25
    assert np.diff([q.t for q in arc]).max() <= 0.25 + 1e-9
    for q in arc:
        assert sep(q.az, q.alt, fn(q.t)) >= 16.0 - 0.05
    for q in p.points:  # and the whole path stays outside the circle
        assert sep(q.az, q.alt, fn(q.t)) >= 16.0 - 0.05
    assert p.min_sun_sep_deg >= 16.0 - 0.05
    assert min(q.alt for q in arc) >= LIM + 0.5
    # the plan resumes the object's own track on both sides of the arc
    pre = [q for q in p.points if q.t < d.t_a]
    post = [q for q in p.points[:-1] if q.t > d.t_b]  # (the last point is t_out)
    assert pre and post and all(q.t == round(q.t) for q in pre + post)
    assert np.diff([q.t for q in p.points]).min() > 0
    # axis 0 stays continuous through the detour and the rates are within 0.8 vmax
    x = np.array([q.axis0 for q in p.points])
    t = np.array([q.t for q in p.points])
    assert np.abs(np.diff(x) / np.diff(t)).max() <= 0.8 * 20.0 + 1e-6
    assert p.detour_seconds == pytest.approx(d.t_b - d.t_a)


def test_detour_side_follows_the_shorter_way_round():
    ref = ok(LEO, MAIN_NOW)
    sides = []
    for left in (True, False):
        sun = sun_beside(ref, 0.5, 4.0, left=left)
        p = ok(LEO, MAIN_NOW, sun=lambda t, s=sun: s)
        d = p.detours[0]
        # the track's own bearing sweep round the Sun over [t_a, t_b] decides the shorter side
        ra = np.interp([d.t_a, d.t_b], [q.t for q in ref.points], [q.az for q in ref.points])
        rh = np.interp([d.t_a, d.t_b], [q.t for q in ref.points], [q.alt for q in ref.points])
        sweep = (bearing(ra[1], rh[1], sun) - bearing(ra[0], rh[0], sun) + math.pi) % (
            2 * math.pi
        ) - math.pi
        assert d.side == ("cw" if sweep > 0 else "ccw")
        assert d.length_deg < 180.0
        sides.append(d.side)
    assert sides[0] != sides[1]


def test_arc_candidates_are_rejected_for_lim_el_and_the_keyhole():
    s = setup(LEO)
    sun = _Sun(lambda t: (18.0, 90.0), 0.0)
    # circle round a Sun at 18 deg: its lower half is below Lim EL + 0.5, the upper half is fine
    left = unproject(-16.0, 0.0, (18.0, 90.0))
    right = unproject(16.0, 0.0, (18.0, 90.0))
    got = {side: _arc(s, sun, 0.0, 30.0, left, right, side) for side in ("cw", "ccw")}
    reasons = {side: why for side, (c, why) in got.items()}
    assert sorted(reasons.values()) == ["", "below Lim EL"]
    for side, (c, _why) in got.items():
        if c is not None:
            assert min(c.alt) >= LIM + 0.5 and c.side == side
    # Sun at 74: the circle just reaches the zenith, the way over the top is above the keyhole
    hi = _Sun(lambda t: (74.0, 90.0), 0.0)
    left = unproject(-16.0, 0.0, (74.0, 90.0))
    right = unproject(16.0, 0.0, (74.0, 90.0))
    got = {side: _arc(s, hi, 0.0, 60.0, left, right, side)[1] for side in ("cw", "ccw")}
    assert "keyhole" in got.values()


FAST = replace(M, ax0=AxisParams(100.0, 100.0), ax1=AxisParams(100.0, 100.0))


def zenith_with_sun(lim: float, lateral: float):
    """The near-zenith pass with a Sun `lateral` deg beside it where it climbs through 74 deg."""
    now = T0 + 70
    info = info_at(ZENITH_89, now, lim)
    base = plan_track(setup(ZENITH_89, mount=FAST, lim=lim), info, now)
    assert isinstance(base, TrackPlan)
    i = next(i for i, q in enumerate(base.points) if q.alt >= 74.0)
    a, b = base.points[i], base.points[i + 1]
    x, y = project(b.az, b.alt, (a.alt, a.az))
    n = math.hypot(x, y)
    az, alt = unproject(-lateral * y / n, lateral * x / n, (a.alt, a.az))
    return plan_track(setup(ZENITH_89, sun=lambda t: (alt, az), mount=FAST, lim=lim), info, now)


def test_no_detour_below_lim_el_and_at_the_keyhole():
    assert R_NO_DETOUR == "Pass passes within 15° of the Sun; no safe detour"
    # Lim EL 60: the way under the Sun dips below it, the way over is too fast for the axes
    res = zenith_with_sun(60.0, 8.0)
    assert isinstance(res, TrackRefusal) and (res.reason, res.code) == (
        R_NO_DETOUR,
        "sun_no_detour",
    )
    assert "below Lim EL" in res.detail
    # Lim EL 55 and a Sun 4 deg beside the climb: no way is left either; one is the keyhole
    res = zenith_with_sun(55.0, 4.0)
    assert isinstance(res, TrackRefusal) and res.code == "sun_no_detour"
    assert "keyhole" in res.detail
    # further out there is a way round
    res = zenith_with_sun(60.0, 12.0)
    assert isinstance(res, TrackPlan) and len(res.detours) == 1 and res.min_sun_sep_deg >= 15.95


def test_rendezvous_inside_a_blocked_interval_moves_to_its_end():
    ref = ok(LEO, MAIN_NOW)
    sun = sun_beside(ref, 0.0, 6.0)  # right beside the planned rendezvous point
    p = ok(LEO, MAIN_NOW, sun=lambda t: sun)
    assert p.t_r > ref.t_r + 10.0
    assert sep(p.rendezvous.az, p.rendezvous.alt, sun) >= 16.0 - 0.05
    assert not p.detours or p.detours[0].t_a > p.t_r
    assert p.rendezvous.alt >= LIM + 0.5
    # the same shifted point as the first second the object is 16 deg from the Sun
    prev = next(q for q in ref.points if q.t == p.t_r - 1)
    assert sep(prev.az, prev.alt, sun) < 16.0 + 0.1


def test_rendezvous_shift_that_leaves_under_30_s_refuses():
    # a slow (MEO) object 3 deg from the Sun: the interval covers the whole 100 s left of the pass
    now = T0 + 60000
    info = replace(info_at(MEO, now), t_out=now + 100.0)
    ref = plan_track(setup(MEO, mount=FAST), info, now)
    assert isinstance(ref, TrackPlan)
    sun = sun_beside(ref, 0.5, 3.0)
    res = plan_track(setup(MEO, mount=FAST, sun=lambda t: sun), info, now)
    assert isinstance(res, TrackRefusal) and (res.reason, res.code) == (
        R_TOO_SHORT,
        "pass_too_short",
    )


def test_a_track_ending_inside_the_disc_is_refused():
    ref = ok(LEO, MAIN_NOW)
    end_sun = sun_beside(ref, 1.0 - 1e-9, 2.0)
    res = plan_track(setup(LEO, sun=lambda t: end_sun), info_at(LEO, MAIN_NOW), MAIN_NOW)
    assert isinstance(res, TrackRefusal) and res.code == "sun_no_detour"
    assert "ends inside the disc" in res.detail


def test_pass_end_between_15_and_16_deg_is_projected_onto_the_circle():
    ref = ok(LEO, MAIN_NOW)
    end = ref.points[-1]
    sun = unproject(15.5, 0.0, (end.alt, end.az))  # (az, alt)
    sun = (sun[1], sun[0])
    p = ok(LEO, MAIN_NOW, sun=lambda t: sun)
    assert p.detours and p.detours[-1].t_b == pytest.approx(p.t_end)
    last = p.points[-1]
    assert sep(last.az, last.alt, sun) == pytest.approx(16.0, abs=0.05)
    assert p.min_sun_sep_deg >= 16.0 - 0.05


def test_far_sun_costs_nothing_and_missing_sun_is_ignored():
    p = ok(LEO, MAIN_NOW, sun=lambda t: (-60.0, 200.0))
    assert p.detours == [] and p.min_sun_sep_deg > 60
    q = ok(LEO, MAIN_NOW, sun=lambda t: None)
    assert q.detours == [] and q.min_sun_sep_deg is None
    assert [x.t for x in q.points] == [x.t for x in p.points]


def test_sun_interpolation_stays_exact_where_its_azimuth_races():
    # a Sun 2 deg from the zenith turning 0.5 deg/s in azimuth: alt/az would interpolate badly
    exact = lambda t: (88.0, (t - T0) * 0.5 % 360.0)  # noqa: E731
    sun = _Sun(exact, T0)
    for t in (T0 + 7.0, T0 + 29.9, T0 + 100.4, T0 + 211.0):
        alt, az = sun.at(t)
        assert sep(az, alt, exact(t)) < 0.03  # (the sagitta of a 30 s chord on a 2 deg circle)
    ts_ = np.array([T0 + 5.0, T0 + 150.0, T0 + 299.0])
    v = sun.vec(ts_, 30.0)
    for k, t in enumerate(ts_):
        e = exact(float(t))
        c = float(
            np.dot(
                v[k],
                [
                    math.cos(math.radians(e[0])) * math.cos(math.radians(e[1])),
                    math.cos(math.radians(e[0])) * math.sin(math.radians(e[1])),
                    math.sin(math.radians(e[0])),
                ],
            )
        )
        assert math.degrees(math.acos(min(1.0, c / np.linalg.norm(v[k])))) < 0.05


# ------------------------------------------------------------------ chunks


def test_never_setting_object_is_planned_in_chunks():
    now = T0
    info = info_at(GEO, now)
    assert info.t_out is None
    s = setup(GEO)
    p = plan_track(s, info, now)
    assert isinstance(p, TrackPlan) and p.final is False
    assert p.t_end - p.t_r == pytest.approx(CHUNK_S)
    assert len(p.points) == CHUNK_S + 1
    nxt = extend_track(s, p.t_end, p.points[-1].axis0)
    assert isinstance(nxt, TrackPlan) and nxt.final is False
    assert nxt.t_r == p.t_end + 1.0 and nxt.t_end - nxt.t_r == pytest.approx(CHUNK_S)
    assert abs(nxt.points[0].axis0 - p.points[-1].axis0) < 0.05  # continuous axis 0
    assert nxt.corridor[0] == (nxt.points[0].axis0, nxt.points[0].alt)
    # the next chunk continues the SAME track
    t = np.array([nxt.points[0].t])
    alt, _ = _altaz(EarthSatellite(*GEO, ts=TS), TOPOS, TS, t)
    assert nxt.points[0].alt == pytest.approx(refract_alt(float(alt[0])), abs=0.001)


def test_long_finite_pass_is_chunked_until_the_lim_el_exit():
    now = T0 + 60000
    info = info_at(MEO, now)
    t_out = now + 4000.5  # shortened (the real pass lasts hours); not a whole second
    info = replace(info, t_out=t_out)
    s = setup(MEO)
    plan = plan_track(s, info, now)
    assert isinstance(plan, TrackPlan) and plan.final is False
    times = [q.t for q in plan.points]
    x_last = plan.points[-1].axis0
    t_end = plan.t_end
    chunks = 1
    while not plan.final:
        plan = extend_track(s, t_end, x_last, t_out=t_out)
        assert isinstance(plan, TrackPlan)
        assert plan.t_r == t_end + 1.0
        assert abs(plan.points[0].axis0 - x_last) < 0.5
        times += [q.t for q in plan.points]
        x_last, t_end = plan.points[-1].axis0, plan.t_end
        chunks += 1
    assert chunks == 3
    assert times[-1] == pytest.approx(t_out, abs=1e-6)
    assert np.diff(times[:-1]).min() == 1.0 and np.diff(times).min() > 0


def test_extend_refuses_when_the_object_has_set():
    info = info_at(LEO, MAIN_NOW)
    res = extend_track(setup(LEO), info.t_out + 20.0, 0.0)
    assert isinstance(res, TrackRefusal) and res.code == "setting"


@pytest.mark.parametrize("slack", [600.0, 0.0])  # 0: the grid has to grow (EXTEND_S)
def test_a_chunk_never_ends_inside_a_sun_interval(monkeypatch, slack):
    import gateway.trackplan as tp

    monkeypatch.setattr(tp, "CHUNK_S", 120.0)  # short chunks on the LEO pass
    monkeypatch.setattr(tp, "RDV_SLACK_S", slack)
    info = info_at(LEO, MAIN_NOW)
    ref = plan_track(setup(LEO), info, MAIN_NOW)
    assert isinstance(ref, TrackPlan) and not ref.final and 100.0 <= ref.t_end - ref.t_r <= 120.0
    sun = sun_beside(ref, 1.0 - 1e-9, 3.0)  # beside the object at the nominal chunk end
    p = plan_track(setup(LEO, sun=lambda t: sun), info, MAIN_NOW)
    assert isinstance(p, TrackPlan) and not p.final
    assert p.t_end > ref.t_end  # the chunk grew until the object was clear of the Sun again
    last = p.points[-1]
    assert sep(last.az, last.alt, sun) >= 16.0 - 0.05
    assert len(p.detours) == 1 and p.detours[0].t_b <= p.t_end
    # and the next chunk starts clear of it, continuing axis 0
    nxt = extend_track(setup(LEO, sun=lambda t: sun), p.t_end, p.points[-1].axis0)
    assert isinstance(nxt, TrackPlan) and nxt.t_r == p.t_end + 1.0
    assert abs(nxt.points[0].axis0 - p.points[-1].axis0) < 3.0


# ------------------------------------------------------------------ T4.2f: Sun stop


def drifting_sun(ref_pt, t_enter: float, rate: float = 0.005):
    """A Sun sweeping across the (almost fixed) sky point `ref_pt`: its distance to the point is
    16 deg at `t_enter`, then shrinks at `rate` deg/s (a slow object drifting into the Sun)."""
    centre = (ref_pt.alt, ref_pt.az)

    def fn(t):
        az, alt = unproject(16.0 + (t_enter - t) * rate, 0.0, centre)
        return (alt, az)

    return fn


def test_slow_object_drifting_into_the_sun_ends_at_the_16_deg_entry():
    from gateway.trackplan import R_SUN_APPROACHING, SUN_HOLD_S

    now = T0
    info = info_at(GEO, now)
    ref = plan_track(setup(GEO), info, now)
    t_enter = now + 900.0
    fn = drifting_sun(ref.points[0], t_enter)
    p = plan_track(setup(GEO, sun=fn), info, now)
    assert isinstance(p, TrackPlan) and p.final is True and p.detours == []
    assert p.end_reason == R_SUN_APPROACHING
    assert p.t_end == p.points[-1].t == pytest.approx(t_enter, abs=2.0)
    n_min = int((p.t_end - p.t_r) // 60)
    assert f"Sun blocks the track in {n_min} min" in p.notes and n_min >= 1
    assert p.t_end - p.t_r > 0 and SUN_HOLD_S == 600
    # never closer than 16 deg along the path
    assert min(sep(q.az, q.alt, fn(q.t)) for q in p.points) >= 16.0 - 0.05
    assert p.min_sun_sep_deg >= 16.0 - 0.05


def test_normal_ends_keep_the_default_reason_and_no_note():
    p = ok(LEO, MAIN_NOW)
    assert p.end_reason == "Track ended at Lim EL" and p.notes == []


def test_leo_with_a_short_sun_interval_still_detours():
    ref = ok(LEO, MAIN_NOW)
    sun = sun_beside(ref, 0.5, 3.0)
    p = ok(LEO, MAIN_NOW, sun=lambda t: sun)
    assert len(p.detours) == 1 and p.end_reason == "Track ended at Lim EL" and p.final
    assert not any("Sun blocks" in n for n in p.notes)


def test_rendezvous_inside_a_long_sun_interval_is_refused():
    info = info_at(GEO, T0)
    ref = plan_track(setup(GEO), info, T0)
    fn = drifting_sun(ref.points[0], T0 - 60.0)  # already inside the 16 deg circle, 15.7 deg out
    res = plan_track(setup(GEO, sun=fn), info, T0)
    assert isinstance(res, TrackRefusal) and res.code == "sun_no_detour"
    # < 30 s of track before the interval: too short
    fn = drifting_sun(ref.points[0], T0 + 25.0)
    res = plan_track(setup(GEO, sun=fn), info, T0)
    assert isinstance(res, TrackRefusal) and res.code == "pass_too_short"


def test_extend_track_meets_the_sun_and_ends_final():
    from gateway.trackplan import R_SUN_APPROACHING

    now = T0
    info = info_at(GEO, now)
    ref = plan_track(setup(GEO), info, now)
    t_enter = now + 2400.0  # beyond the first 30 min chunk
    fn = drifting_sun(ref.points[0], t_enter)
    s = setup(GEO, sun=fn)
    p = plan_track(s, info, now)
    assert isinstance(p, TrackPlan) and not p.final and p.end_reason == "Track ended at Lim EL"
    nxt = extend_track(s, p.t_end, p.points[-1].axis0)
    assert isinstance(nxt, TrackPlan) and nxt.final and nxt.end_reason == R_SUN_APPROACHING
    assert nxt.t_end == pytest.approx(t_enter, abs=2.0)
    assert nxt.t_r == p.t_end + 1.0 and nxt.detours == []
    # a chunk that starts inside the interval: the track simply ends there
    res = extend_track(s, t_enter + 100.0, p.points[-1].axis0)
    assert isinstance(res, TrackRefusal) and res.reason == R_SUN_APPROACHING


EPH_PATH = Path(__file__).resolve().parents[2] / "data" / "ephemeris" / "de421.bsp"


@pytest.mark.skipif(not EPH_PATH.exists(), reason="no local ephemeris")
def test_thaicom_8_drifting_into_the_real_sun():
    """The user's live check (2026-10-29, Bangkok): THAICOM 8 at 59.8 deg is 18.3 deg from the
    Sun at 05:46 UTC and approaching."""
    from datetime import UTC, datetime

    from skyfield.api import wgs84

    from gateway.ephem import Ephem
    from gateway.passes import compute_pass, track_verdict
    from gateway.trackplan import R_SUN_APPROACHING

    eph = Ephem(EPH_PATH)
    ts = eph.timescale
    site = (13.1199722222222, 100.920694444444, 12.0)
    tle = (
        "1 41552U 16031A   26301.63911513 -.00000057  00000-0  00000-0 0  9992",
        "2 41552   0.0899  44.8113 0000754 249.3674  51.4291  1.00271698 38059",
    )

    def sun_at(t):
        return eph.sun_altaz(site, datetime.fromtimestamp(t, UTC))

    topos = wgs84.latlon(site[0], site[1], elevation_m=site[2])
    sat = EarthSatellite(*tle, ts=ts)

    def entry_brute(now):
        t = np.arange(now, now + 7200.0, 1.0)
        alt, az = _altaz(sat, topos, ts, t)
        alt = np.array([refract_alt(float(a)) for a in alt])
        sp = np.array([sep(az[k], alt[k], sun_at(float(t[k]))) for k in range(0, len(t), 5)])
        j = int(np.nonzero(sp < 16.0)[0][0]) * 5
        lo = max(j - 5, 0)
        for k in range(lo, j + 1):
            if sep(az[k], alt[k], sun_at(float(t[k]))) < 16.0:
                return float(t[k])
        raise AssertionError

    def run(hh, mm):
        now = datetime(2026, 10, 29, hh, mm, tzinfo=UTC).timestamp()
        info = compute_pass(*tle, site, 10.0, now, lambda: sun_at(now), ts)
        assert track_verdict(info, now, 10.0) == (None, None, "track")
        return now, plan_track(TrackSetup(*tle, site, 10.0, ts, sun_at, M), info, now)

    now, p = run(5, 46)
    assert isinstance(p, TrackPlan) and p.final and p.end_reason == R_SUN_APPROACHING
    assert p.t_end == pytest.approx(entry_brute(now), abs=2.0)
    assert any(n.startswith("Sun blocks the track in ") for n in p.notes)
    now2, p2 = run(5, 55)
    assert isinstance(p2, TrackPlan) and p2.final and p2.end_reason == R_SUN_APPROACHING
    assert abs(p2.t_end - p.t_end) < 5.0  # the same entry, less track left
    assert p2.t_end - p2.t_r < p.t_end - p.t_r
    # 06:00 the object is already inside 16 deg (15.7 deg): refused, not 15 deg yet
    now3 = datetime(2026, 10, 29, 6, 0, tzinfo=UTC).timestamp()
    info3 = compute_pass(*tle, site, 10.0, now3, lambda: sun_at(now3), ts)
    res = plan_track(TrackSetup(*tle, site, 10.0, ts, sun_at, M), info3, now3)
    assert isinstance(res, TrackRefusal) and res.code == "sun_no_detour"


# ------------------------------------------------------------------ budget


def timed(s, info, now):
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        p = plan_track(s, info, now)
        best = min(best, time.perf_counter() - t0)
    return p, best


def test_budget_with_a_detour(capsys):
    # (a) the LEO pass down to Lim EL 2 deg: ~7 min
    info = info_at(LEO, MAIN_NOW, 2.0)
    ref = plan_track(setup(LEO, lim=2.0), info, MAIN_NOW)
    sun = sun_beside(ref, 0.5, 3.0)
    p, best = timed(setup(LEO, sun=lambda t: sun, lim=2.0), info, MAIN_NOW)
    assert isinstance(p, TrackPlan) and len(p.detours) == 1
    # (b) a 15 min stretch of the MEO pass (about 900 points); the Sun is far (a slow object
    # sitting 3 deg from the Sun would block the whole stretch)
    now = T0 + 60000
    info15 = replace(info_at(MEO, now), t_out=now + 900.5)
    p15, best15 = timed(setup(MEO, mount=FAST), info15, now)
    assert isinstance(p15, TrackPlan) and p15.final and len(p15.points) > 880
    with capsys.disabled():
        print(
            f"\n  plan_track: LEO {len(p.points)} points + 1 detour {best * 1000:.0f} ms; "
            f"15 min / {len(p15.points)} points, no detour {best15 * 1000:.0f} ms (best of 5)"
        )
    assert best < 0.25 and best15 < 0.25  # the 100 ms budget with CI headroom


def test_planning_is_pure_and_deterministic():
    a = ok(LEO, MAIN_NOW)
    b = ok(LEO, MAIN_NOW)
    assert a == b


def test_peak70_synthetic_pass_plans():
    info = info_at(PEAK_70, T0 + 100)
    assert isinstance(info, PassInfo) and info.peak_alt == pytest.approx(70.0, abs=0.5)
    p = ok(PEAK_70, T0 + 100)
    assert p.notes == [] and p.final
