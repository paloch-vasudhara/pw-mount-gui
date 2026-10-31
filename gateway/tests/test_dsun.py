"""T3.5b: D-SUN planner. Pure-function tests (no I/O)."""

from __future__ import annotations

import math
import random
import time

import pytest

from gateway.dsun import (
    CRUISE_DEG_S,
    KEYHOLE_ALT_DEG,
    LIM_EL_PAD_DEG,
    R_DETOUR_DEG,
    RATE_FRACTION,
    NoRoute,
    Route,
    _arc,
    plan_route,
    project,
    route_rates,
    unproject,
)
from gateway.safety import AxisParams, sun_sep

AX = AxisParams(20.0, 20.0)
RANGE = (-300.0, 300.0)


def plan(start, target, sun, lim=15.0, ax0=AX, ax1=AX, rng=RANGE, a1max=90.0, **kw):
    return plan_route(start, target, sun, lim, ax0, ax1, rng, a1max, **kw)


def angdiff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def check_invariants(r: Route, start, target, sun, lim, ax0, ax1, rng=RANGE):
    pts = r.points
    assert (pts[0].axis0, pts[0].alt, pts[0].t) == (start[0], start[1], 0.0)
    assert (pts[-1].axis0, pts[-1].alt) == (target[0], target[1])
    assert r.min_sun_sep_deg >= R_DETOUR_DEG - 0.05
    for a, b in zip(pts, pts[1:], strict=False):
        assert b.t > a.t
        assert b.t - a.t <= 0.5 + 1e-9
        assert abs(b.axis0 - a.axis0) < 4.0  # <= cruise (15) * POINT_DT_S (0.25): no 360 jumps
    for p in pts:
        assert p.alt >= lim + LIM_EL_PAD_DEG - 1e-9 or p in (pts[0], pts[-1])
        assert 0.0 <= p.az < 360.0
        assert angdiff(p.az, p.axis0) < 1e-9
        assert sun_sep(p.az, p.alt, sun) >= R_DETOUR_DEG - 0.5  # chords of 0.5 s samples
        assert rng[0] <= p.axis0 <= rng[1]
    r0, r1 = route_rates(r)
    assert r0 <= RATE_FRACTION * ax0.vmax * 1.01
    assert r1 <= RATE_FRACTION * ax1.vmax * 1.01
    assert r.corridor[0] == (start[0], start[1])
    assert r.corridor[-1] == (target[0], target[1])


# ------------------------------------------------------------------ projection


@pytest.mark.parametrize(
    "sun",
    [(45.0, 180.0), (89.0, 10.0), (20.0, 0.5), (60.0, 359.0), (80.0, 270.0)],
)
def test_projection_round_trip(sun):
    for az, alt in [(359.0, 30.0), (1.0, 30.0), (180.0, 50.0), (90.0, 70.0), (270.0, 20.0)]:
        x, y = project(az, alt, sun)
        if math.hypot(x, y) < 0.5:
            continue
        az2, alt2 = unproject(x, y, sun)
        assert alt2 == pytest.approx(alt, abs=1e-8)
        assert angdiff(az2, az) < 1e-7


def test_projection_radius_is_separation():
    sun = (45.0, 180.0)
    x, y = project(190.0, 50.0, sun)
    assert math.hypot(x, y) == pytest.approx(sun_sep(190.0, 50.0, sun), abs=1e-9)
    assert project(180.0, 45.0, sun) == (0.0, 0.0)


# ------------------------------------------------------------------ routing cases


def test_no_detour_returns_none():
    assert plan((100.0, 40.0), (150.0, 40.0), (45.0, 250.0)) is None
    # passes at 17 deg: outside R (16)
    assert plan((150.0, 62.0), (210.0, 62.0), (45.0, 180.0)) is None


def test_start_and_target_in_disc():
    sun = (45.0, 180.0)
    r = plan((185.0, 45.0), (250.0, 45.0), sun)
    assert isinstance(r, NoRoute) and r.reason == "start_in_disc"
    r = plan((100.0, 45.0), (185.0, 45.0), sun)
    assert isinstance(r, NoRoute) and r.reason == "target_in_disc"


def test_straight_slew_through_disc_picks_cheaper_side():
    sun = (45.0, 180.0)
    start, target = (130.0, 45.0), (230.0, 45.0)
    r = plan(start, target, sun)
    assert isinstance(r, Route)
    assert r.intervals == 1 and r.sides[0] in ("cw", "ccw")
    check_invariants(r, start, target, sun, 15.0, AX, AX)
    # the detour is ~ half the circle; the chosen side is not longer than the other one
    other = "ccw" if r.sides[0] == "cw" else "cw"
    assert r.detour_deg > 0
    assert other != r.sides[0]
    assert (
        r.duration_s > 100.0 / CRUISE_DEG_S
    )  # slower than the bare cruise over the nominal length


def test_asymmetric_case_picks_lower_side():
    # Sun at 70 deg: an arc over the top stretches azimuth much more than the one below
    sun = (70.0, 180.0)
    start, target = (120.0, 70.0), (240.0, 70.0)
    r = plan(start, target, sun)
    assert isinstance(r, Route)
    check_invariants(r, start, target, sun, 15.0, AX, AX)
    assert max(p.alt for p in r.points) <= 70.0 + 1e-6
    assert min(p.alt for p in r.points) < 60.0


def test_sun_near_zenith_discards_over_zenith_side():
    sun = (80.0, 180.0)
    start, target = (120.0, 68.0), (240.0, 68.0)
    r = plan(start, target, sun)
    assert isinstance(r, Route)
    check_invariants(r, start, target, sun, 15.0, AX, AX)
    assert max(p.alt for p in r.points) <= 68.0 + 1e-6
    # direct: both arcs of one interval, exactly one is usable
    b_in, b_out = math.radians(-60.0), math.radians(60.0)
    first = unproject(R_DETOUR_DEG * math.sin(b_in), R_DETOUR_DEG * math.cos(b_in), sun)
    last = unproject(R_DETOUR_DEG * math.sin(b_out), R_DETOUR_DEG * math.cos(b_out), sun)
    reasons = {s: _arc(first, last, s, sun, 15.0, KEYHOLE_ALT_DEG, RANGE)[1] for s in ("cw", "ccw")}
    # the over-zenith one is discarded (keyhole, or the az winding flips over the zenith)
    assert sorted(bool(v) for v in reasons.values()) == [False, True]


def test_sun_near_lim_el_discards_low_side():
    sun = (20.0, 180.0)
    start, target = (130.0, 20.0), (230.0, 20.0)
    r = plan(start, target, sun, lim=15.0)
    assert isinstance(r, Route)
    check_invariants(r, start, target, sun, 15.0, AX, AX)
    assert min(p.alt for p in r.points) >= 15.0 + LIM_EL_PAD_DEG - 1e-9
    assert max(p.alt for p in r.points) > 30.0


def test_both_sides_blocked_is_no_route():
    sun = (30.0, 180.0)
    r = plan((130.0, 30.0), (230.0, 30.0), sun, lim=15.0, a1max=40.0)
    assert isinstance(r, NoRoute)
    assert r.reason == "no_detour"
    assert "Lim EL" in r.detail and "keyhole" in r.detail


def test_axis0_range_discards_candidates():
    sun = (45.0, 180.0)
    r = plan((130.0, 45.0), (230.0, 45.0), sun, rng=(100.0, 250.0))
    assert isinstance(r, Route)  # the arcs stay within 164..196: still inside
    r = plan((130.0, 45.0), (230.0, 45.0), sun, rng=(100.0, 170.0))
    assert isinstance(r, NoRoute) and "axis0 range" in r.detail


def test_wrap_turn_sweep():
    sun = (40.0, 100.0)
    start, target = (-60.0, 40.0), (300.0, 40.0)  # same azimuth, 360 deg sweep through the Sun
    r = plan(start, target, sun, rng=(-310.0, 310.0))
    assert isinstance(r, Route)
    check_invariants(r, start, target, sun, 15.0, AX, AX, rng=(-310.0, 310.0))
    xs = [p.axis0 for p in r.points]
    assert min(xs) >= -60.0 - 1.0 and max(xs) <= 300.0 + 1.0
    # monotone-ish: never backs up more than the detour arc's own wiggle
    back = max(a - b for a, b in zip(xs, xs[1:], strict=False))
    assert back < 1.5


def test_endpoint_inside_detour_circle_uses_radial_connection():
    sun = (45.0, 180.0)
    az, alt = unproject(15.5 * math.sin(math.radians(100)), 15.5 * math.cos(math.radians(100)), sun)
    start = (az, alt)
    target = (260.0, 45.0)
    assert 15.0 < sun_sep(az, alt, sun) < R_DETOUR_DEG
    r = plan(start, target, sun)
    assert isinstance(r, Route)
    assert r.points[0].axis0 == start[0]
    assert r.min_sun_sep_deg >= 15.0 - 1e-6
    assert all(sun_sep(p.az, p.alt, sun) >= 15.0 for p in r.points)
    assert r.points[-1].axis0 == target[0]
    # target inside 16 but outside 15
    r2 = plan((100.0, 45.0), start, sun)
    assert isinstance(r2, Route) and (r2.points[-1].axis0, r2.points[-1].alt) == start


def test_high_alt_rate_constraint():
    sun = (85.0, 180.0)
    start, target = (120.0, 70.0), (240.0, 70.0)
    fast = plan(start, target, sun)
    slow_ax = AxisParams(4.0, 20.0)
    slow = plan(start, target, sun, ax0=slow_ax)
    assert isinstance(fast, Route) and isinstance(slow, Route)
    check_invariants(fast, start, target, sun, 15.0, AX, AX)
    check_invariants(slow, start, target, sun, 15.0, slow_ax, AX)
    assert route_rates(slow)[0] <= RATE_FRACTION * 4.0 * 1.01
    assert slow.duration_s > fast.duration_s
    # an axis too slow to follow the route at all: no route
    crawl = plan(start, target, sun, ax0=AxisParams(0.2, 20.0))
    assert isinstance(crawl, NoRoute)


def test_corner_slows_down_and_accel_limits_start():
    sun = (45.0, 180.0)
    r = plan((130.0, 45.0), (230.0, 45.0), sun, ax0=AxisParams(20.0, 2.0))
    assert isinstance(r, Route)
    # a = 2 deg/s^2: after 1 s the speed (and so the step) is small
    p = r.points
    assert math.hypot(p[1].axis0 - p[0].axis0, p[1].alt - p[0].alt) < 1.0


# ------------------------------------------------------------------ invariants sweep


def _random_case(rnd: random.Random):
    sun = (rnd.uniform(25.0, 70.0), rnd.uniform(0.0, 360.0))
    while True:
        s = (rnd.uniform(-250.0, 250.0), rnd.uniform(22.0, 80.0))
        t = (rnd.uniform(-250.0, 250.0), rnd.uniform(22.0, 80.0))
        if sun_sep(s[0] % 360.0, s[1], sun) >= 16.5 and sun_sep(t[0] % 360.0, t[1], sun) >= 16.5:
            return sun, s, t


def test_invariants_sweep():
    rnd = random.Random(35)
    routes = 0
    nones = 0
    norts = 0
    for _ in range(200):
        sun, s, t = _random_case(rnd)
        ax0 = AxisParams(rnd.uniform(6.0, 20.0), rnd.uniform(5.0, 20.0))
        ax1 = AxisParams(rnd.uniform(6.0, 20.0), rnd.uniform(5.0, 20.0))
        r = plan(s, t, sun, ax0=ax0, ax1=ax1)
        if r is None:
            nones += 1
            continue
        if isinstance(r, NoRoute):
            assert r.reason == "no_detour"
            norts += 1
            continue
        routes += 1
        check_invariants(r, s, t, sun, 15.0, ax0, ax1)
    assert routes >= 20, (routes, nones, norts)


def test_wrap_sweep_cases_every_sun_azimuth():
    for k in range(12):
        sun = (35.0 + 2.0 * k, 30.0 * k)
        start, target = (-60.0, 50.0), (300.0, 50.0)
        r = plan(start, target, sun, rng=(-310.0, 310.0))
        if r is None:
            continue
        if isinstance(r, Route):
            check_invariants(r, start, target, sun, 15.0, AX, AX, rng=(-310.0, 310.0))


def test_performance():
    sun = (40.0, 100.0)
    t0 = time.perf_counter()
    r = plan((-60.0, 40.0), (300.0, 40.0), sun, rng=(-310.0, 310.0))
    dt = time.perf_counter() - t0
    assert isinstance(r, Route)
    assert dt < 0.2, dt
    t0 = time.perf_counter()
    plan((130.0, 45.0), (230.0, 45.0), (45.0, 180.0))
    assert time.perf_counter() - t0 < 0.2


def test_force_returns_timed_straight_route_when_clear() -> None:
    """Supervisor (T3.5b): the predictor can flag a slew whose straight axis-space line is clear;
    `force` then gives that line as a timed route with no detour."""
    pr = plan_route
    ax = AxisParams(20.0, 20.0)
    start, target, sun = (100.0, 40.0), (160.0, 40.0), (75.0, 130.0)
    assert pr(start, target, sun, 15.0, ax, ax, (-300.0, 300.0), 91.0) is None
    r = pr(start, target, sun, 15.0, ax, ax, (-300.0, 300.0), 91.0, force=True)
    assert isinstance(r, Route) and r.intervals == 0 and r.sides == []
    assert (r.points[0].axis0, r.points[0].alt) == start
    assert (r.points[-1].axis0, r.points[-1].alt) == target
    assert abs(r.detour_deg) < 1e-6 and r.min_sun_sep_deg >= 16.0
