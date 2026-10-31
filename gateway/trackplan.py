"""Object-track planner (T4.2b): the timed alt/az path the gateway flies for TRACK OBJECT.
Pure: no I/O, no clock; the timescale, the Sun, the mount pose and its limits come in as
arguments.

Spec: BUILD_PLAN section 6 "T4.2 / 1. Track planner". The path is the object's own alt/az from
sgp4 every 1 s (geometric via skyfield as in `passes.py`, then PWI4's refraction model, P18/P22),
starting at the RENDEZVOUS time `t_r` and ending at the Lim EL exit (`t_out`), or after one chunk
(30 min) for objects that do not set soon: `extend_track` plans the next chunk later.

  * Rendezvous (one rule for both phases): `t_r = max(t_in, now + slew_estimate + 3 s)`, iterated
    3 times, then pushed to the first second the object is at least Lim EL + 0.5 and outside any
    D-SUN blocked interval; at least 30 s must remain before `t_out`.
  * D-SUN for the track: where the object comes within 16 deg of the Sun (R_DETOUR_DEG) the
    track points are replaced by an arc on the 16 deg circle, from the entry to the exit point,
    timed uniformly over [t_a, t_b] with a point every 0.25 s. The arc follows the Sun's own
    motion (each point is placed in the Sun-centred frame of ITS time), so the clearance is 16 deg
    all along the arc, not only at the mid time. Clockwise / counter-clockwise: the shorter one that
    stays >= Lim EL + 0.5, <= the keyhole, within 0.8 vmax per axis and ARC_CRUISE_DEG_S.
  * Sun stop (T4.2f, user decision Q20): a blocked interval longer than SUN_HOLD_S (a slow object
    drifting into the Sun, e.g. a GEO) is not detoured: the plan ends at its start with the
    end_reason "Track ended: Sun approaching" (refused only if < 30 s of track would remain). A
    rendezvous inside such an interval is refused (sun_no_detour).
  * The track times are whole Unix seconds (the planner grid), the last point of a finite pass is
    exactly `t_out`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .dsun import (
    ARC_CRUISE_DEG_S,
    KEYHOLE_ALT_DEG,
    LIM_EL_PAD_DEG,
    POINT_DT_S,
    R_DETOUR_DEG,
    RATE_FRACTION,
    project,
    unproject,
)
from .ephem import angular_sep_deg
from .passes import (
    MIN_TRACK_S,
    R_PEAK_BELOW,
    R_SETTING,
    R_TOO_SHORT,
    SUN_KEEPOUT_DEG,
    PassInfo,
    _altaz,
    _make,
)
from .safety import (
    SLEW_TAU_DEFAULT_S,
    AxisParams,
    axis0_target,
    turn_duration_s,
)
from .tle import CODE_TLE_UNAVAILABLE, R_TLE_UNAVAILABLE
from .transforms import refract_alt

TRACK_STEP_S = 1.0  # P22: 1 s track points
CHUNK_S = 1800.0  # first chunk of a long track (the executor extends every 10 min)
RDV_SLACK_S = 600.0  # the grid reaches this far beyond a chunk (slew time, Sun shifts)
EXTEND_S = 600.0  # the grid grows by this much when a chunk would end inside a blocked interval
RDV_MARGIN_S = 3.0
RDV_ITERATIONS = 3
ZENITH_NOTE_ALT_DEG = 87.0
SUN_KNOT_S = 30.0  # the Sun is evaluated on this lattice and interpolated (it moves 0.004 deg/s)
SUN_COARSE_S = 300.0
SUN_PRESCREEN_DEG = 20.0
SUN_HOLD_S = 600  # a blocked interval longer than this is a "Sun stop", not a detour candidate

R_ZENITH_NOTE = "Near-zenith pass: tracking lags near the peak"
R_NO_DETOUR = "Pass passes within 15° of the Sun; no safe detour"
R_TRACK_ENDED = "Track ended at Lim EL"
R_SUN_APPROACHING = "Track ended: Sun approaching"
R_NO_AZ_RANGE = "Pass leaves the mount's azimuth range"

CODE_NO_DETOUR = "sun_no_detour"
CODE_TOO_SHORT = "pass_too_short"
CODE_SETTING = "setting"
CODE_PEAK_BELOW = "peak_below_limit"
CODE_NO_AZ = "no_az"
CODE_SUN_STOP = "sun_stop"  # extend_track only: the next chunk starts inside a long Sun interval

_INF_RANGE = (-1.0e9, 1.0e9)


# ------------------------------------------------------------------ data


@dataclass(frozen=True)
class TrackPoint:
    t: float  # Unix seconds
    az: float  # 0..360, PWI4's refracted frame
    alt: float
    axis0: float  # continuous (unwrapped) axis-0 position


@dataclass(frozen=True)
class Detour:
    t_a: float
    t_b: float
    side: str  # "cw" / "ccw"
    length_deg: float  # sky length of the arc


@dataclass
class TrackPlan:
    points: list[TrackPoint]
    t_r: float  # rendezvous time (= points[0].t)
    rendezvous: TrackPoint
    t_end: float  # time of the last point of THIS chunk
    final: (
        bool  # True: the path ends at the Lim EL exit; False: `extend_track` plans the next chunk
    )
    detours: list[Detour]
    min_sun_sep_deg: float | None
    notes: list[str]
    corridor: list[tuple[float, float]]  # (axis0, alt) polyline for the monitor
    end_reason: str = R_TRACK_ENDED  # R_SUN_APPROACHING when the plan ends at a long Sun interval

    @property
    def detour_seconds(self) -> float:
        return sum(d.t_b - d.t_a for d in self.detours)


@dataclass(frozen=True)
class TrackRefusal:
    reason: str
    code: str
    detail: str = ""


@dataclass(frozen=True)
class TrackMount:
    """The mount state and limits the planner needs (from the poller's latest PWI4 status)."""

    axis0: float
    axis1: float
    ax0: AxisParams
    ax1: AxisParams
    axis0_range: tuple[float, float] = _INF_RANGE
    axis1_max: float = 90.0
    tau: float = SLEW_TAU_DEFAULT_S


SunFn = Callable[[float], "tuple[float, float] | None"]  # Unix t -> Sun (alt, az)


@dataclass(frozen=True)
class TrackSetup:
    line1: str
    line2: str
    site: tuple[float, float, float]
    lim_el: float
    ts: Any  # skyfield timescale
    sun_at: SunFn
    mount: TrackMount


def mount_from_status(st: Any) -> TrackMount | None:
    """TrackMount from a PWI4 status (None while the axis positions are unavailable)."""
    m = getattr(st, "mount", None)
    if m is None:
        return None
    a0, a1 = m.axis0, m.axis1
    if a0.position_degs is None or a1.position_degs is None:
        return None
    lo, hi = a0.min_mech_position_degs, a0.max_mech_position_degs
    rng = (lo if lo is not None else _INF_RANGE[0], hi if hi is not None else _INF_RANGE[1])
    max1 = a1.max_mech_position_degs if a1.max_mech_position_degs is not None else 90.0
    return TrackMount(
        a0.position_degs,
        a1.position_degs,
        AxisParams(a0.max_velocity_degs_per_sec or 20.0, a0.acceleration_degs_per_sec_sqr or 20.0),
        AxisParams(a1.max_velocity_degs_per_sec or 20.0, a1.acceleration_degs_per_sec_sqr or 20.0),
        rng,
        max1,
        getattr(m, "slew_time_constant", None) or SLEW_TAU_DEFAULT_S,
    )


def slew_estimate_s(mount: TrackMount, az: float, alt: float) -> float:
    """Estimated duration (s) of a PWI4 goto from the mount's pose to (az, alt): the per-axis
    model of the goto predictor (vmax / accel + the approach constant, P13); axes are independent
    so the slower one rules."""
    tgt0 = axis0_target(mount.axis0, az, mount.axis0_range[0], mount.axis0_range[1])
    if tgt0 is None:
        tgt0 = mount.axis0 + (az - mount.axis0 + 180.0) % 360.0 - 180.0
    t0 = turn_duration_s(mount.axis0, tgt0, mount.ax0.vmax, mount.ax0.accel, mount.tau)
    t1 = turn_duration_s(mount.axis1, alt, mount.ax1.vmax, mount.ax1.accel, mount.tau)
    return max(t0, t1)


# ------------------------------------------------------------------ helpers


def _unwrap(az: np.ndarray) -> np.ndarray:
    """Continuous azimuth. Steps are taken the short way except near +-180 (a near-zenith flip),
    where the pass's dominant sense of rotation decides."""
    az = np.asarray(az, dtype=float)
    if az.size < 2:
        return az.copy()
    d = (np.diff(az) + 180.0) % 360.0 - 180.0
    sense = 1.0 if float(d.sum()) >= 0.0 else -1.0
    d = np.where(d * sense < -150.0, d + 360.0 * sense, d)
    return az[0] + np.concatenate([[0.0], np.cumsum(d)])


def _unit(az: np.ndarray, alt: np.ndarray) -> np.ndarray:
    a, h = np.radians(az), np.radians(alt)
    return np.stack([np.cos(h) * np.cos(a), np.cos(h) * np.sin(a), np.sin(h)], axis=-1)


def _altaz_of(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    v = v / np.linalg.norm(v, axis=-1, keepdims=True)
    alt = np.degrees(np.arcsin(np.clip(v[..., 2], -1.0, 1.0)))
    return alt, np.degrees(np.arctan2(v[..., 1], v[..., 0])) % 360.0


def _sep_vec(az: np.ndarray, alt: np.ndarray, sun_unit: np.ndarray) -> np.ndarray:
    c = np.sum(_unit(az, alt) * sun_unit, axis=-1) / np.linalg.norm(sun_unit, axis=-1)
    return np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))


class _Sun:
    """The Sun as a function of time: `sun_at` evaluated on a lattice and interpolated linearly
    as a UNIT VECTOR (0.004 deg/s: the error over 30 s is far below 0.001 deg, and the vector
    stays smooth where the Sun's azimuth races past the zenith). `ok` False = no Sun
    (ephemeris unavailable): no Sun checks."""

    def __init__(self, fn: SunFn, t0: float) -> None:
        self.fn = fn
        self.cache: dict[float, np.ndarray | None] = {}
        self.ok = self.knot(math.floor(t0 / SUN_KNOT_S) * SUN_KNOT_S) is not None

    def knot(self, tk: float) -> np.ndarray | None:
        if tk not in self.cache:
            p = self.fn(tk)
            self.cache[tk] = None if p is None else _unit(np.array(p[1]), np.array(p[0]))
        return self.cache[tk]

    def at(self, t: float) -> tuple[float, float]:
        """Sun (alt, az) at t."""
        k = math.floor(t / SUN_KNOT_S)
        a, b = self.knot(k * SUN_KNOT_S), self.knot((k + 1) * SUN_KNOT_S)
        assert a is not None and b is not None
        f = (t - k * SUN_KNOT_S) / SUN_KNOT_S
        alt, az = _altaz_of(a + f * (b - a))
        return float(alt), float(az)

    def vec(self, t: np.ndarray, step: float) -> np.ndarray:
        """Sun unit vector at each time, interpolated on a lattice of `step` s."""
        k0, k1 = math.floor(float(t.min()) / step), math.ceil(float(t.max()) / step)
        if k1 == k0:
            k1 += 1
        tk = np.arange(k0, k1 + 1) * step
        kn = np.array([self.knot(float(x)) for x in tk])
        return np.stack([np.interp(t, tk, kn[:, i]) for i in range(3)], axis=-1)


def _sep(az: float, alt: float, sun: tuple[float, float]) -> float:
    return angular_sep_deg(az, alt, sun[1], sun[0])


@dataclass
class _Candidate:
    side: str
    az: list[float]
    alt: list[float]
    t: list[float]
    length_deg: float


def _arc(
    s: TrackSetup,
    sun: _Sun,
    t_a: float,
    t_b: float,
    pa: tuple[float, float],
    pb: tuple[float, float],
    side: str,
) -> tuple[_Candidate | None, str]:
    """One detour candidate from pa (az, alt) at t_a to pb at t_b along the 16 deg circle."""
    sa, sb = sun.at(t_a), sun.at(t_b)
    ba = math.atan2(*project(pa[0], pa[1], sa))
    bb = math.atan2(*project(pb[0], pb[1], sb))
    d = (bb - ba) % (2.0 * math.pi)
    if side == "ccw":
        d -= 2.0 * math.pi
    n = max(1, math.ceil((t_b - t_a) / POINT_DT_S - 1e-9))
    ts_, azs, alts = [], [], []
    for k in range(n + 1):
        f = k / n
        t = t_a + (t_b - t_a) * f
        b = ba + d * f
        az, alt = unproject(R_DETOUR_DEG * math.sin(b), R_DETOUR_DEG * math.cos(b), sun.at(t))
        ts_.append(t)
        azs.append(az)
        alts.append(alt)
    azs[0], alts[0], azs[-1], alts[-1] = pa[0], pa[1], pb[0], pb[1]
    m = s.mount
    # The arc stays >= Lim EL + 0.5; but its ends sit on the track, which near the Lim EL exit is
    # itself lower than that: then no point may go below the lower end.
    if min(alts) < min(s.lim_el + LIM_EL_PAD_DEG, pa[1], pb[1]) - 1e-6:
        return None, "below Lim EL"
    # (a track that itself passes the keyhole altitude: its ends may be above it)
    if max(alts) > max(min(KEYHOLE_ALT_DEG, m.axis1_max), pa[1], pb[1]) + 1e-6:
        return None, "keyhole"
    if n >= 1:
        dt = (t_b - t_a) / n
        ax = _unwrap(np.array(azs))
        v0 = np.abs(np.diff(ax)) / dt
        v1 = np.abs(np.diff(np.array(alts))) / dt
        if (
            float(v0.max()) > RATE_FRACTION * m.ax0.vmax
            or float(v1.max()) > RATE_FRACTION * m.ax1.vmax
        ):
            return None, "rate limit"
        if float(np.hypot(v0, v1).max()) > ARC_CRUISE_DEG_S:
            return None, "arc speed"
    return _Candidate(side, azs, alts, ts_, abs(d) * R_DETOUR_DEG), ""


_MORE = object()  # the grid has to grow: the chunk would end inside a blocked interval


# ------------------------------------------------------------------ the planner


def _attempt(
    s: TrackSetup,
    t_lo: float,
    win_end: float,
    t_out: float | None,
    now: float,
    rdv: bool,
    axis0_ref: float,
    notes: list[str],
):
    # --- the object's refracted alt/az on the whole-second grid
    t0 = math.ceil(t_lo - 1e-9)
    T = np.arange(t0, math.floor(win_end + 1e-9) + 1, dtype=float)
    if win_end - float(T[-1]) > 1e-6:
        T = np.append(T, win_end)
    if T.size < 2:
        return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
    sat, topos = _make(s.line1, s.line2, s.site, s.ts)
    alt_g, az_g = _altaz(sat, topos, s.ts, T)
    if not (np.all(np.isfinite(alt_g)) and np.all(np.isfinite(az_g))):
        return TrackRefusal(R_TLE_UNAVAILABLE, CODE_TLE_UNAVAILABLE)
    ALT = np.array([refract_alt(float(a)) for a in alt_g])
    AZ = az_g % 360.0
    AZU = _unwrap(AZ)
    n = len(T)
    lim = s.lim_el
    pad = lim + LIM_EL_PAD_DEG

    # --- the Sun along the grid: prescreen on a coarse lattice, refine where it is near
    sun = _Sun(s.sun_at, float(T[0]))
    sepv: np.ndarray | None = None
    if sun.ok:
        sepv = _sep_vec(AZ, ALT, sun.vec(T, SUN_COARSE_S))
        near = np.nonzero(sepv < SUN_PRESCREEN_DEG)[0]
        if near.size:
            a, b = int(near[0]), int(near[-1])
            sepv[a : b + 1] = _sep_vec(
                AZ[a : b + 1], ALT[a : b + 1], sun.vec(T[a : b + 1], SUN_KNOT_S)
            )
    blocked = (sepv < R_DETOUR_DEG) if sepv is not None else np.zeros(n, dtype=bool)
    # all blocked runs of the grid; the long ones (> SUN_HOLD_S) are Sun stops
    edges = np.diff(np.concatenate([[0], blocked.astype(np.int8), [0]]))
    all_runs = list(
        zip(
            np.nonzero(edges == 1)[0].tolist(),
            (np.nonzero(edges == -1)[0] - 1).tolist(),
            strict=True,
        )
    )
    long_runs = [(i0, i1) for i0, i1 in all_runs if i1 - i0 + 1 > SUN_HOLD_S]

    def in_long_run(i: int) -> bool:
        return any(i0 <= i <= i1 for i0, i1 in long_runs)

    def obj_at(t: float) -> tuple[float, float]:
        return float(np.interp(t, T, AZU)) % 360.0, float(np.interp(t, T, ALT))

    def clear_of_sun(i: int) -> int | None:
        while i < n and blocked[i]:
            i += 1
        return i if i < n else None

    def track_at(t: float) -> tuple[float, float]:
        return obj_at(t)

    def crossing(i_out: int, i_in: int) -> float:
        """Time between grid samples i_out (>= R from the Sun) and i_in (< R) the track crosses
        the 16 deg circle (the side with separation >= R)."""
        lo, hi = float(T[i_out]), float(T[i_in])
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if _sep(*track_at(mid), sun.at(mid)) >= R_DETOUR_DEG:
                lo = mid
            else:
                hi = mid
        return lo

    # --- the rendezvous
    if rdv:
        up = np.nonzero(ALT >= pad)[0]
        if not up.size:
            return TrackRefusal(R_PEAK_BELOW, CODE_PEAK_BELOW)
        t_min = max(float(T[0]), float(T[int(up[0])]))
        t_r = t_min

        def reachable(i0: int) -> int | None:
            """First grid sample from i0 the mount can reach in time (slew + margin). A scan,
            not a fixed-point iteration: near the zenith the object's azimuth moves faster than
            the mount and the iteration oscillated onto unreachable points (T4.2d)."""
            for i in range(i0, n):
                ti = float(T[i])
                if now + slew_estimate_s(s.mount, *obj_at(ti)) + RDV_MARGIN_S <= ti:
                    return i
            return None

        ir = 0
        for _ in range(4):  # the slew scan, then the Sun shifts settle
            i_r = reachable(int(np.searchsorted(T, max(t_min, now + RDV_MARGIN_S) - 1e-9)))
            if i_r is None:
                return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
            ir = i_r
            t_r = float(T[ir])
            if ir < n and in_long_run(ir):
                return TrackRefusal(R_NO_DETOUR, CODE_NO_DETOUR, "the Sun blocks the rendezvous")
            j = clear_of_sun(ir) if ir < n else None
            if j is None:
                return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
            if j == ir:
                break
            t_min = t_r = float(T[j])
        else:
            return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
        t_r = float(T[ir])
        if t_out is not None and t_out - t_r < MIN_TRACK_S:
            return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
    else:
        if in_long_run(0):
            return TrackRefusal(R_SUN_APPROACHING, CODE_SUN_STOP)
        ir = clear_of_sun(0)
        if ir is None:
            return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
        if ALT[ir] < lim:
            return TrackRefusal(R_SETTING, CODE_SETTING)
        t_r = float(T[ir])

    # --- the chunk end
    reached = t_out is not None and float(T[-1]) >= t_out - 1e-6
    ends_final = reached and t_r + CHUNK_S >= t_out - 1e-6  # type: ignore[operator]
    ie_cand = (
        n - 1
        if ends_final
        else min(n - 1, int(np.searchsorted(T, t_r + CHUNK_S, side="right")) - 1)
    )
    stop = next((i0 for i0, _ in long_runs if i0 > ir and i0 <= ie_cand), None)
    sun_stop_t: float | None = None
    end_reason = R_TRACK_ENDED
    if stop is not None:
        # a long Sun interval starts inside this chunk: end the track at its start (final)
        sun_stop_t = crossing(stop - 1, stop)
        final, ie = True, stop - 1
        end_reason = R_SUN_APPROACHING
        if rdv and sun_stop_t - t_r < MIN_TRACK_S:
            return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
        if ie <= ir and sun_stop_t - float(T[ir]) < 1e-6:
            return TrackRefusal(R_SUN_APPROACHING, CODE_SUN_STOP)
        notes = [*notes, f"Sun blocks the track in {max(1, int((sun_stop_t - t_r) // 60))} min"]
    elif ends_final:
        final, ie = True, n - 1
    else:
        final = False
        ie = ie_cand
        while blocked[ie] and ie < n - 1:
            ie += 1
        if blocked[ie]:
            return _MORE
    if ie <= ir and sun_stop_t is None:
        return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)

    # --- blocked intervals inside [ir, ie]
    runs: list[tuple[int, int]] = []
    i = ir
    while i <= ie:
        if blocked[i]:
            j = i
            while j + 1 <= ie and blocked[j + 1]:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1

    detours: list[Detour] = []
    arcs: list[tuple[int, int, _Candidate]] = []
    arc_min_sep = math.inf
    for i0, i1 in runs:
        t_a = crossing(i0 - 1, i0)
        pa = track_at(t_a)
        if i1 == ie:
            # the chunk (the pass) ends inside the interval: there is no exit point; the arc ends
            # at the radial projection of the last track point onto the circle
            t_b = float(T[ie])
            end_sep = _sep(AZ[ie], ALT[ie], sun.at(t_b))
            if end_sep < SUN_KEEPOUT_DEG:
                return TrackRefusal(R_NO_DETOUR, CODE_NO_DETOUR, "the track ends inside the disc")
            bearing = math.atan2(*project(AZ[ie], ALT[ie], sun.at(t_b)))
            sn = sun.at(t_b)
            pb = unproject(R_DETOUR_DEG * math.sin(bearing), R_DETOUR_DEG * math.cos(bearing), sn)
        else:
            t_b = crossing(i1 + 1, i1)
            pb = track_at(t_b)
        cands, why = [], {}
        for side in ("cw", "ccw"):
            c, w = _arc(s, sun, t_a, t_b, pa, pb, side)
            if c is None:
                why[side] = w
            else:
                cands.append(c)
        if not cands:
            return TrackRefusal(R_NO_DETOUR, CODE_NO_DETOUR, f"cw {why['cw']}, ccw {why['ccw']}")
        best = min(cands, key=lambda c: c.length_deg)
        arcs.append((i0, i1, best))
        detours.append(Detour(t_a, t_b, best.side, best.length_deg))
        for az, alt, t in zip(best.az, best.alt, best.t, strict=True):
            arc_min_sep = min(arc_min_sep, _sep(az, alt, sun.at(t)))

    # --- assemble: grid points outside the intervals + the arcs
    tt: list[float] = []
    az_l: list[float] = []
    alt_l: list[float] = []
    cursor = ir
    for i0, i1, c in arcs:
        tt.extend(T[cursor:i0].tolist())
        az_l.extend(AZ[cursor:i0].tolist())
        alt_l.extend(ALT[cursor:i0].tolist())
        tt.extend(c.t)
        az_l.extend(c.az)
        alt_l.extend(c.alt)
        cursor = i1 + 1
    tt.extend(T[cursor : ie + 1].tolist())
    az_l.extend(AZ[cursor : ie + 1].tolist())
    alt_l.extend(ALT[cursor : ie + 1].tolist())
    if sun_stop_t is not None and sun_stop_t > tt[-1] + 1e-6:
        e_az, e_alt = track_at(sun_stop_t)  # the last point: the 16 deg entry itself
        tt.append(sun_stop_t)
        az_l.append(e_az)
        alt_l.append(e_alt)

    ax = _unwrap(np.array(az_l))
    lo_r, hi_r = s.mount.axis0_range
    chosen = None
    for k in sorted(range(-4, 5), key=lambda k: abs(float(ax[0]) + 360.0 * k - axis0_ref)):
        if lo_r <= float(ax.min()) + 360.0 * k and float(ax.max()) + 360.0 * k <= hi_r:
            chosen = 360.0 * k
            break
    if chosen is None:
        return TrackRefusal(R_NO_AZ_RANGE, CODE_NO_AZ)
    ax = ax + chosen
    points = [
        TrackPoint(t, a % 360.0, h, x)
        for t, a, h, x in zip(tt, az_l, alt_l, ax.tolist(), strict=True)
    ]

    min_sep: float | None = None
    if sepv is not None:
        keep = np.ones(n, dtype=bool)
        for i0, i1, _ in arcs:
            keep[i0 : i1 + 1] = False
        keep[:ir] = False
        keep[ie + 1 :] = False
        vals = sepv[keep]
        min_sep = min(float(vals.min()) if vals.size else math.inf, arc_min_sep)
        if not math.isfinite(min_sep):
            min_sep = None

    return TrackPlan(
        points=points,
        t_r=points[0].t,
        rendezvous=points[0],
        t_end=points[-1].t,
        final=final,
        detours=detours,
        min_sun_sep_deg=min_sep,
        notes=list(notes),
        corridor=[(p.axis0, p.alt) for p in points],
        end_reason=end_reason,
    )


def _run(
    s: TrackSetup,
    t_lo: float,
    t_out: float | None,
    now: float,
    rdv: bool,
    axis0_ref: float,
    notes: list[str],
) -> TrackPlan | TrackRefusal:
    win_end = t_lo + CHUNK_S + RDV_SLACK_S
    if t_out is not None:
        win_end = min(win_end, t_out)
    try:
        for _ in range(4):
            res = _attempt(s, t_lo, win_end, t_out, now, rdv, axis0_ref, notes)
            if res is not _MORE:
                return res
            win_end += EXTEND_S
            if t_out is not None:
                win_end = min(win_end, t_out)
        return TrackRefusal(R_NO_DETOUR, CODE_NO_DETOUR, "no clear point to end the chunk")
    except (ValueError, FloatingPointError):  # sgp4 failure / malformed lines
        return TrackRefusal(R_TLE_UNAVAILABLE, CODE_TLE_UNAVAILABLE)


def plan_track(s: TrackSetup, info: PassInfo, now: float) -> TrackPlan | TrackRefusal:
    """The track for the pass `info` (from `compute_pass`) as seen at `now`: rendezvous, the
    object's refracted path, Sun detours. A finite pass is planned to its Lim EL exit (the first
    chunk, 30 min, when it is longer: `plan.final` False, then `extend_track`)."""
    t_lo = max(info.t_in if info.t_in is not None else now, now)
    if info.t_out is not None and info.t_out - t_lo < MIN_TRACK_S:
        return TrackRefusal(R_TOO_SHORT, CODE_TOO_SHORT)
    notes = []
    if info.peak_alt is not None and info.peak_alt > ZENITH_NOTE_ALT_DEG:
        notes.append(R_ZENITH_NOTE)
    return _run(s, t_lo, info.t_out, now, True, s.mount.axis0, notes)


def extend_track(
    s: TrackSetup,
    t_from: float,
    axis0_ref: float,
    t_out: float | None = None,
) -> TrackPlan | TrackRefusal:
    """The next chunk after a plan that ended at `t_from` (the previous `plan.t_end`): points on
    the whole seconds after it, up to 30 min (or the Lim EL exit `t_out`), starting at the
    object's position and continuing axis 0 from `axis0_ref` (the previous plan's last axis0).
    Refuses "setting" when the object has dropped below Lim EL (the executor stops the track).
    `plan.t_r` here is the first point's time: later than t_from + 1 only when the object is
    inside a Sun interval then (the executor must treat a gap as a stop)."""
    t_lo = math.floor(t_from + 1e-9) + 1.0
    return _run(s, t_lo, t_out, t_lo, False, axis0_ref, [])
