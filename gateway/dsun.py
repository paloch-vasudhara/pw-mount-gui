"""D-SUN planner (T3.5b): pure Sun-avoidance route planning. No I/O, no asyncio, no PWI4.

Spec: BUILD_PLAN section 4 "D-SUN" and section 6 "T3.5 / Planner". Wiring into safety / control
is T3.5c; this module only turns (start, target, Sun, limits, axis rates) into a timed route.

Spaces
  * AXIS space = (axis0 unwrapped deg, axis1 = altitude deg): where a PWI4 slew is a straight
    line and where the nominal route lives.
  * SKY = (az 0..360, alt). `sun` is (alt, az) like everywhere in safety.py.
  * TANGENT plane = azimuthal-equidistant projection centred on the Sun (`project` /
    `unproject`): plane radius = angular distance, plane angle = bearing from the Sun.

Algorithm (`plan_route`)
  1. Nominal route = straight axis-space line start -> target sampled every <= NOMINAL_STEP_DEG.
  2. Blocked intervals = contiguous runs with Sun separation < R_DETOUR_DEG (16 deg). Entry and
     exit points are the 16 deg crossings (bisection).
  3. Per interval two candidates run along the 16 deg circle from entry to exit: "cw" = the
     bearing from the Sun increases (clockwise on a north-up sky chart), "ccw" = decreases. A
     candidate is discarded when an arc point is below lim_el + LIM_EL_PAD_DEG, above
     min(KEYHOLE_ALT_DEG, axis1_max), outside `axis0_range`, when the arc ends on a different
     axis-0 winding than the nominal exit point, or when the route cannot be timed (an axis
     would need a path speed below MIN_PATH_V_DEG_S to stay within RATE_FRACTION * vmax).
  4. The candidate giving the shorter timed route wins (intervals are decided in order; later
     intervals use their geometrically shorter valid candidate while an earlier one is judged).
  5. Timing: path speed (axis space) per segment = min(cruise (straight parts) / arc_cruise (detour
     arcs), RATE_FRACTION * vmax of every axis
     scaled by the segment direction); vertex speeds are 0 at both ends and <= CORNER_V_DEG_S at
     corners with a heading change > CORNER_DEG; forward / backward passes enforce the reported
     accelerations along the path. Segments are constant-acceleration between vertex speeds.
     The route is then resampled every <= POINT_DT_S (t = 0 is the start; the caller adds LEAD_S).

Return value
  * None            : the nominal line never comes within R_DETOUR_DEG of the Sun: no detour needed
                      (with `force` a timed straight Route with `intervals == 0` instead).
  * NoRoute(reason) : "start_in_disc" / "target_in_disc" (< 15 deg, the caller's business) or
                      "no_detour" (no valid candidate for some interval; `detail` says why).
  * Route           : the timed detour route.

Endpoint inside 16 deg (but outside 15 deg): the arc starts / ends at the RADIAL projection of
that endpoint onto the 16 deg circle (same bearing from the Sun, radius 16) and the route joins
it with a straight axis-space segment (it moves away from the Sun). Such a route's
`min_sun_sep_deg` is therefore between 15 and 16 near that endpoint.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from .ephem import angular_sep_deg
from .safety import KEEPOUT_DEG, AxisParams, sun_sep

R_DETOUR_DEG = 16.0  # KEEPOUT 15 + O7 margin 1
LIM_EL_PAD_DEG = 0.5
KEYHOLE_ALT_DEG = 88.0
RATE_FRACTION = 0.8
# Path speeds (user 10-28: the 5 deg/s first cut was too slow). P21 measured following error on the
# sim: straight lines <= 0.06 deg mean at 10-15 deg/s; the r = 16 deg arc 0.13 deg at 10 deg/s with
# 0.25 s points. Both are far inside the 1 deg margin (16 vs 15 deg) and the 2 deg corridor.
CRUISE_DEG_S = 15.0  # straight (nominal) parts
ARC_CRUISE_DEG_S = 10.0  # detour arcs
LEAD_S = 2.0  # used by the caller (first point = current pose at t0 + LEAD)
POINT_DT_S = 0.25  # 0.25 s points halve the chord error on arcs (P21)
ARC_STEP_DEG = 1.0
NOMINAL_STEP_DEG = 0.25
CORNER_DEG = 30.0
CORNER_V_DEG_S = 1.0  # path speed at a sharp corner
MIN_PATH_V_DEG_S = 0.25  # slower than this to respect the rate limit -> candidate unusable

_EPS = 1e-9


@dataclass(frozen=True)
class RoutePoint:
    t: float  # s since the route start
    az: float  # 0..360
    alt: float
    axis0: float  # unwrapped


@dataclass
class Route:
    points: list[RoutePoint]
    duration_s: float
    detour_deg: float  # axis-space length minus the nominal line length
    sides: list[str]  # "cw" / "ccw" per blocked interval
    intervals: int
    min_sun_sep_deg: float  # over the dense polyline
    corridor: list[tuple[float, float]] = field(default_factory=list)  # axis-space polyline


@dataclass(frozen=True)
class NoRoute:
    reason: str  # "no_detour" | "start_in_disc" | "target_in_disc"
    detail: str = ""


# ------------------------------------------------------------------ projection


def project(az: float, alt: float, sun: tuple[float, float]) -> tuple[float, float]:
    """Sky (az, alt) -> tangent-plane (x, y) in degrees, centred on the Sun (alt, az).
    Plane radius = angular distance, plane angle = bearing (x = c sin b, y = c cos b)."""
    c, b = _polar(az, alt, sun)
    return c * math.sin(b), c * math.cos(b)


def unproject(x: float, y: float, sun: tuple[float, float]) -> tuple[float, float]:
    """Inverse of `project`: plane (x, y) -> sky (az 0..360, alt)."""
    return _from_polar(math.hypot(x, y), math.atan2(x, y), sun)


def _polar(az: float, alt: float, sun: tuple[float, float]) -> tuple[float, float]:
    """(angular distance deg, bearing rad from the Sun) of a sky point."""
    a0, z0 = math.radians(sun[0]), math.radians(sun[1])
    h, z = math.radians(alt), math.radians(az)
    dz = z - z0
    c = angular_sep_deg(az, alt, sun[1], sun[0])
    b = math.atan2(
        math.sin(dz) * math.cos(h),
        math.cos(a0) * math.sin(h) - math.sin(a0) * math.cos(h) * math.cos(dz),
    )
    return c, b


def _from_polar(c_deg: float, b: float, sun: tuple[float, float]) -> tuple[float, float]:
    a0, z0 = math.radians(sun[0]), math.radians(sun[1])
    c = math.radians(c_deg)
    sin_h = math.sin(a0) * math.cos(c) + math.cos(a0) * math.sin(c) * math.cos(b)
    h = math.asin(max(-1.0, min(1.0, sin_h)))
    dz = math.atan2(
        math.sin(b) * math.sin(c) * math.cos(a0),
        math.cos(c) - math.sin(a0) * math.sin(h),
    )
    return math.degrees(z0 + dz) % 360.0, math.degrees(h)


def _nearest(az: float, ref: float) -> float:
    """az + 360k nearest to `ref`."""
    return az + 360.0 * round((ref - az) / 360.0)


# ------------------------------------------------------------------ geometry helpers


def _sep(p: tuple[float, float], sun: tuple[float, float]) -> float:
    return sun_sep(p[0] % 360.0, p[1], sun)


def _lerp(a: tuple[float, float], b: tuple[float, float], s: float) -> tuple[float, float]:
    return (a[0] + s * (b[0] - a[0]), a[1] + s * (b[1] - a[1]))


def _length(pts: list[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:], strict=False))


def _radial(p: tuple[float, float], sun: tuple[float, float]) -> tuple[float, float]:
    """Radial projection of an axis-space point onto the detour circle (axis0 nearest to p)."""
    _, b = _polar(p[0] % 360.0, p[1], sun)
    az, alt = _from_polar(R_DETOUR_DEG, b, sun)
    return (_nearest(az, p[0]), alt)


def _crossing(
    a: tuple[float, float], b: tuple[float, float], sun: tuple[float, float]
) -> tuple[float, float]:
    """Point where the segment a(outside) -> b(inside) crosses R_DETOUR_DEG, or b(outside) <- a;
    returned from the OUTSIDE side of the bisection (separation >= R). Order-agnostic."""
    out, ins = (a, b) if _sep(a, sun) >= R_DETOUR_DEG else (b, a)
    lo, hi = 0.0, 1.0  # lo = outside, hi = inside (parameter from `out` to `ins`)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if _sep(_lerp(out, ins, mid), sun) >= R_DETOUR_DEG:
            lo = mid
        else:
            hi = mid
    return _lerp(out, ins, lo)


def _arc(
    first: tuple[float, float],
    last: tuple[float, float],
    side: str,
    sun: tuple[float, float],
    lim_el: float,
    alt_max: float,
    axis0_range: tuple[float, float],
) -> tuple[list[tuple[float, float]] | None, str]:
    """Axis-space arc along the detour circle from `first` to `last` (both on / radially on the
    circle). Returns (points incl. both ends, "") or (None, reason)."""
    _, b_in = _polar(first[0] % 360.0, first[1], sun)
    _, b_out = _polar(last[0] % 360.0, last[1], sun)
    d = (b_out - b_in) % (2.0 * math.pi)
    if side == "ccw":
        d -= 2.0 * math.pi
    n = max(1, math.ceil(abs(math.degrees(d)) / ARC_STEP_DEG - _EPS))
    pts = [first]
    x_prev = first[0]
    for i in range(1, n + 1):
        az, alt = _from_polar(R_DETOUR_DEG, b_in + d * i / n, sun)
        x = _nearest(az, x_prev)
        x_prev = x
        pts.append((x, alt))
    if abs(pts[-1][0] - last[0]) > 0.5:
        return None, "axis0 winding"
    pts[-1] = last
    pad = lim_el + LIM_EL_PAD_DEG
    for x, y in pts:
        if y < pad:
            return None, "below Lim EL"
        if y > alt_max:
            return None, "keyhole"
        if not axis0_range[0] <= x <= axis0_range[1]:
            return None, "axis0 range"
    return pts, ""


# ------------------------------------------------------------------ timing


def _dedupe(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-7:
            out.append(p)
    return out


@dataclass
class _Timed:
    pts: list[tuple[float, float]]
    t: list[float]  # time at each vertex
    v: list[float]  # path speed at each vertex
    length: list[float]  # segment lengths

    @property
    def duration(self) -> float:
        return self.t[-1]


def _time_polyline(
    pts: list[tuple[float, float]],
    ax0: AxisParams,
    ax1: AxisParams,
    cruise: float,
    arc: list[bool] | None = None,
    arc_cruise: float = ARC_CRUISE_DEG_S,
) -> _Timed | None:
    """Assign times to a deduplicated polyline. None when a segment would need a path speed
    below MIN_PATH_V_DEG_S to keep both axes within RATE_FRACTION * vmax."""
    n = len(pts)
    if n < 2:
        return _Timed(pts, [0.0] * n, [0.0] * n, [])
    seg_len, seg_cap, seg_acc = [], [], []
    for j, (a, b) in enumerate(zip(pts, pts[1:], strict=False)):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        ln = math.hypot(dx, dy)
        on_arc = arc is not None and arc[j] and arc[j + 1]
        cap, acc = (min(cruise, arc_cruise) if on_arc else cruise), math.inf
        for dv, ax in ((dx, ax0), (dy, ax1)):
            if dv > 1e-12:
                cap = min(cap, RATE_FRACTION * ax.vmax * ln / dv)
                acc = min(acc, max(ax.accel, 1e-3) * ln / dv)
        if cap < MIN_PATH_V_DEG_S:
            return None
        seg_len.append(ln)
        seg_cap.append(cap)
        seg_acc.append(acc)
    v = [0.0] * n
    for j in range(1, n - 1):
        v[j] = min(seg_cap[j - 1], seg_cap[j])
        a, b, c = pts[j - 1], pts[j], pts[j + 1]
        h1 = math.atan2(b[1] - a[1], b[0] - a[0])
        h2 = math.atan2(c[1] - b[1], c[0] - b[0])
        turn = abs((math.degrees(h2 - h1) + 180.0) % 360.0 - 180.0)
        if turn > CORNER_DEG:
            v[j] = min(v[j], CORNER_V_DEG_S)
    for j in range(n - 1):  # forward: reachable speed
        v[j + 1] = min(v[j + 1], math.sqrt(v[j] ** 2 + 2.0 * seg_acc[j] * seg_len[j]))
    for j in range(n - 2, -1, -1):  # backward: stoppable speed
        v[j] = min(v[j], math.sqrt(v[j + 1] ** 2 + 2.0 * seg_acc[j] * seg_len[j]))
    t = [0.0]
    for j in range(n - 1):
        s = v[j] + v[j + 1]
        if s < 1e-9:  # both ends at rest (a single short segment): rest-to-rest
            dt = 2.0 * math.sqrt(seg_len[j] / max(min(seg_acc[j], 1e6), 1e-6))
        else:
            dt = 2.0 * seg_len[j] / s
        t.append(t[-1] + dt)
    return _Timed(pts, t, v, seg_len)


def _resample(tm: _Timed, dt: float) -> list[tuple[float, float, float]]:
    """(t, axis0, alt) every <= dt, first = start, last = end of the polyline."""
    out = [(0.0, tm.pts[0][0], tm.pts[0][1])]
    total = tm.duration
    k = 1
    while k * dt < total - 1e-6:
        tt = k * dt
        j = min(bisect.bisect_right(tm.t, tt) - 1, len(tm.pts) - 2)
        seg_t = tm.t[j + 1] - tm.t[j]
        tau = tt - tm.t[j]
        s = tm.v[j] * tau + (tm.v[j + 1] - tm.v[j]) / (2.0 * seg_t) * tau * tau
        f = max(0.0, min(1.0, s / tm.length[j])) if tm.length[j] > 0 else 0.0
        p = _lerp(tm.pts[j], tm.pts[j + 1], f)
        out.append((tt, p[0], p[1]))
        k += 1
    out.append((total, tm.pts[-1][0], tm.pts[-1][1]))
    return out


# ------------------------------------------------------------------ planner


@dataclass
class _Interval:
    i0: int
    i1: int
    x_in: tuple[float, float] | None  # None: the start is inside R
    x_out: tuple[float, float] | None  # None: the target is inside R
    arcs: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    why: dict[str, str] = field(default_factory=dict)


def _assemble(
    samples: list[tuple[float, float]], ivs: list[_Interval], sides: list[str]
) -> tuple[list[tuple[float, float]], list[bool]]:
    """(polyline, per-point "on a detour arc" flag; a segment is an arc segment when both of its
    ends are arc points, so the straight lead-in / radial legs keep the straight cruise)."""
    pts: list[tuple[float, float]] = []
    arc: list[bool] = []

    def add(ps: list[tuple[float, float]], on_arc: bool) -> None:
        for p in ps:
            if pts and math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) <= 1e-7:
                arc[-1] = arc[-1] or on_arc  # duplicate point (dedupe)
                continue
            pts.append(p)
            arc.append(on_arc)

    cursor = 0
    n = len(samples) - 1
    for iv, side in zip(ivs, sides, strict=True):
        add(samples[cursor : iv.i0], False)  # unblocked samples before the run
        if iv.x_in is None:
            add([samples[0]], False)
        add(iv.arcs[side], True)
        if iv.x_out is None:
            add([samples[n]], False)
        cursor = iv.i1 + 1
    add(samples[cursor:], False)
    return pts, arc


def plan_route(
    start: tuple[float, float],
    target: tuple[float, float],
    sun: tuple[float, float],
    lim_el: float,
    ax0: AxisParams,
    ax1: AxisParams,
    axis0_range: tuple[float, float],
    axis1_max: float,
    cruise: float = CRUISE_DEG_S,
    force: bool = False,
    arc_cruise: float = ARC_CRUISE_DEG_S,
) -> Route | NoRoute | None:
    """Plan a Sun-avoiding route; see the module docstring. start / target are (axis0, alt).

    `force`: return a timed Route (no detour intervals) instead of None when the nominal line is
    clear. The caller uses it when the goto PREDICTOR flags the slew (its per-axis timing makes a
    dog-leg that crosses the disc) but the straight axis-space line does not: flying that line as
    a custom path avoids the crossing without a detour (T3.5b supervisor check: ~20 % of the
    predictor-flagged random slews)."""
    if _sep(start, sun) < KEEPOUT_DEG:
        return NoRoute("start_in_disc", f"start {_sep(start, sun):.2f} deg from the Sun")
    if _sep(target, sun) < KEEPOUT_DEG:
        return NoRoute("target_in_disc", f"target {_sep(target, sun):.2f} deg from the Sun")

    nominal_len = math.hypot(target[0] - start[0], target[1] - start[1])
    n = max(2, math.ceil(nominal_len / NOMINAL_STEP_DEG))
    samples = [_lerp(start, target, i / n) for i in range(n + 1)]
    blocked = [_sep(p, sun) < R_DETOUR_DEG for p in samples]
    if not any(blocked) and not force:
        return None

    alt_max = min(KEYHOLE_ALT_DEG, axis1_max)
    ivs: list[_Interval] = []
    i = 0
    while i <= n:
        if not blocked[i]:
            i += 1
            continue
        j = i
        while j + 1 <= n and blocked[j + 1]:
            j += 1
        x_in = _crossing(samples[i - 1], samples[i], sun) if i > 0 else None
        x_out = _crossing(samples[j], samples[j + 1], sun) if j < n else None
        iv = _Interval(i, j, x_in, x_out)
        first = x_in if x_in is not None else _radial(start, sun)
        last = x_out if x_out is not None else _radial(target, sun)
        for side in ("cw", "ccw"):
            arc, why = _arc(first, last, side, sun, lim_el, alt_max, axis0_range)
            if arc is None:
                iv.why[side] = why
            else:
                iv.arcs[side] = arc
        ivs.append(iv)
        i = j + 1

    for k, iv in enumerate(ivs):
        if not iv.arcs:
            return NoRoute("no_detour", f"interval {k + 1}: cw {iv.why['cw']}, ccw {iv.why['ccw']}")

    # provisional choice = the geometrically shorter valid arc; then refine by timed duration
    sides = [min(iv.arcs, key=lambda s, iv=iv: _length(iv.arcs[s])) for iv in ivs]
    for k, iv in enumerate(ivs):
        best: tuple[float, str] | None = None
        for side in iv.arcs:
            trial = [*sides[:k], side, *sides[k + 1 :]]
            poly, arc = _assemble(samples, ivs, trial)
            tm = _time_polyline(poly, ax0, ax1, cruise, arc, arc_cruise)
            if tm is None:
                iv.why[side] = "rate limit"
                continue
            if best is None or tm.duration < best[0]:
                best = (tm.duration, side)
        if best is None:
            return NoRoute("no_detour", f"interval {k + 1}: rate limit on every candidate")
        sides[k] = best[1]

    poly, arc = _assemble(samples, ivs, sides)
    tm = _time_polyline(poly, ax0, ax1, cruise, arc, arc_cruise)
    if tm is None:
        return NoRoute("no_detour", "rate limit")

    res = _resample(tm, POINT_DT_S)
    points = [RoutePoint(t, x % 360.0, y, x) for t, x, y in res]
    points[0] = RoutePoint(0.0, start[0] % 360.0, start[1], start[0])
    points[-1] = RoutePoint(tm.duration, target[0] % 360.0, target[1], target[0])
    dense_min = min(_sep(p, sun) for p in poly)
    return Route(
        points=points,
        duration_s=tm.duration,
        detour_deg=_length(poly) - nominal_len,
        sides=sides,
        intervals=len(ivs),
        min_sun_sep_deg=dense_min,
        corridor=[(p.axis0, p.alt) for p in points],
    )


def route_rates(route: Route) -> tuple[float, float]:
    """(max |axis0 rate|, max |axis1 rate|) deg/s between consecutive route points."""
    r0 = r1 = 0.0
    for a, b in zip(route.points, route.points[1:], strict=False):
        dt = b.t - a.t
        if dt <= 0:
            continue
        r0 = max(r0, abs(b.axis0 - a.axis0) / dt)
        r1 = max(r1, abs(b.alt - a.alt) / dt)
    return r0, r1
