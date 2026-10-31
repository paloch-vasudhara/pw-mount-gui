"""Safety engine v1 (T3.1b): target checks, slew-path prediction, Sun shortcut, 10 Hz monitor.

Plugs into the T3.1a seams: `ControlService.check` (command gate) and `SafetyMonitor`
(continuous predictive stop through `ControlService.safety_stop`).

All alt/az and Sun maths use PWI4's own site (from /status), never the Settings location
(O-SITE). Pure functions live at the top so T3.2 (HOME) and T3.5 (D-SUN) can reuse them.

Frame (T3.3b, P18): every alt/az here is the mount's own frame, i.e. OBSERVED (refracted like
PWI4's reported altitude). RA/Dec goto targets go through H4 with refraction (`Ephem.radec_j2000_
altaz`), and the Sun (`Ephem.sun_altaz`, also the snapshot's sun and sun_distance) is refracted by
the same model (tapered to 0 between -1 and -2 deg, so it never blows up near the horizon). The
Sun shortcut (`sun_shortcut`) therefore compares refracted Sun altitude with Lim EL / mount alt.

Test hook: the env var `PW_TEST_SUN_ALTAZ="alt,az"` pins the Sun to a fixed position. It exists
ONLY for the automated tests (sim runs cannot be pointed at the real Sun) and is inert when
unset; when set, the gateway audits `test_sun_override` at start so it cannot go unnoticed.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .audit import AuditLog
from .commands import Command
from .control import ROUTE_SETTLE_S, CommandContext, ControlService, HomeVerdict
from .ephem import Ephem, angular_sep_deg
from .poller import Poller
from .settings import (
    DEFAULT_HOME_ALT,
    DEFAULT_HOME_AZ,
    SUN_KEEPOUT_DEG,
    SettingsStore,
    compute_site_check,
)

KEEPOUT_DEG = SUN_KEEPOUT_DEG  # 15 deg
LOOKAHEAD_S = 0.5  # monitor look-ahead on the measured velocity (the minimum, see lookahead_s)
# T3.4c: the look-ahead grows with speed so that the mount can still be STOPPED before a limit:
# distance needed = v * STOP_LATENCY_S + v^2 / (2 * accel), i.e. v * lookahead_s(v, accel).
# STOP_LATENCY_S budgets /status age (<= 0.1 s poll + HTTP), the 10 Hz monitor tick, the stop
# request and the velocity-estimate lag (VEL_WINDOW_S); measured worst case ~0.3 s.
STOP_LATENCY_S = 0.45
VEL_WINDOW_S = 0.2  # velocity = position change over at least this long
SLEW_TAU_DEFAULT_S = 0.5  # PWI4 slew time constant (P13) when status does not report it
PATH_SAMPLE_S = 0.1
SIM_DT_S = 0.01
PATH_T_MAX_S = 90.0
ARRIVE_DEG = 0.01
NEGLIGIBLE_V = 0.001  # deg/s: below this an axis counts as not moving
SUN_CACHE_S = 1.0
MONITOR_PERIOD_S = 0.1
STOP_REPEAT_S = 1.0  # do not re-fire a safety stop more often than this
INSTANT_ACCEL = 1.0e6  # "no acceleration limit" variant (see predict_paths)
LIMIT_MARGIN_DEG = 0.05  # validated goto: stop when the ACTUAL altitude is this far below the floor
CORRIDOR_TOL_DEG = 2.0  # validated goto: allowed distance from the predicted path (axis space)
ARRIVED_DEG = 0.2  # validated goto ends when the mount is this close to its target axes
PLAN_STILL_S = 0.8  # ... or when it has not moved for this long (stopped by someone else)

# Stall guard (T3.3b, P19a). An axis is "commanded to move" when its OWN distance to its target
# is large (dist_to_target_arcsec; |target_mech_position - position| when that is not reported)
# or, on a real mount, its setpoint velocity is non-zero. It stalls when, while commanded, its
# position stays within STALL_POS_TOL_ARCSEC of where it was STALL_S ago. Per axis: an altitude
# axis that arrived first has a ~0 target distance and is never "commanded". Healthy sidereal
# tracking keeps the target distance at the follow error (a few arcsec), so it is never
# commanded however slowly the axis turns (pole / zenith poses). Thresholds are from the
# simulator; re-tune in the real-mount session.
STALL_S = 2.5  # commanded and not moving for this long
STALL_POS_TOL_ARCSEC = 1.0  # "not moving" = position within this of the reference
STALL_CMD_DIST_ARCSEC = 900.0  # 0.25 deg: well above any follow / settle error
STALL_CMD_SETPOINT_V = 0.05  # deg/s: a real mount's non-zero setpoint velocity also counts

R_SITE = "Station location differs from PWI4's site"
R_SUN = "Within 15° of the Sun"
R_PATH_SUN = "Slew path passes within 15° of the Sun"
R_NO_EPHEM = "Ephemeris unavailable (cannot verify target)"
R_NO_AZ = "Target azimuth is outside the mount's mechanical range"
R_CORRIDOR = "Mount left the validated slew path"
R_NO_DETOUR = "Slew path passes within 15° of the Sun; no safe detour"


def lookahead_s(speed: float, accel: float) -> float:
    """Predictive look-ahead (s) for an axis moving at `speed` deg/s with deceleration `accel`
    deg/s^2: the time over which a stop request issued now still has to be safe."""
    return max(LOOKAHEAD_S, STOP_LATENCY_S + speed / (2.0 * max(accel, 1.0)))


def jog_margin_deg(speed: float, accel: float) -> float:
    """Stopping distance a jog at `speed` needs before any limit (= speed * look-ahead)."""
    return speed * lookahead_s(speed, accel)


def turn_duration_s(x0: float, x1: float, vmax: float, accel: float, tau: float) -> float:
    """Estimated duration (s) of an axis move x0 -> x1 with the reported velocity / acceleration
    and PWI4's exponential approach (P13): the same model the corridor plans use."""
    path = simulate_axis(x0, x1, vmax, accel, tau)
    return round((len(path) - 1) * PATH_SAMPLE_S, 1)


def r_below(lim_el: float) -> str:
    return f"Below station limit elevation (Lim EL {lim_el:g}°)"


def reason_code(reason: str) -> str | None:
    """Stable machine token of a safety refusal (the dry-run check's `code`)."""
    if reason.startswith("Below station limit elevation"):
        return "below_lim_el"
    return {
        R_SITE: "site_mismatch",
        R_SUN: "sun_target",
        R_PATH_SUN: "sun_path",
        R_NO_DETOUR: "sun_no_detour",
        R_NO_EPHEM: "no_ephemeris",
        R_NO_AZ: "no_az",
        "PWI4 not running / not reachable": "no_pwi4",
    }.get(reason)


# ------------------------------------------------------------------ pure functions


def sun_shortcut(sun_alt: float, lim_el: float, mount_alt: float) -> bool:
    """User decision: when the whole Sun disc is below Lim EL (and below where the mount
    currently points, so a recovery move from below Lim EL stays covered) every Sun check
    is skipped."""
    return sun_alt + KEEPOUT_DEG < min(lim_el, mount_alt)


def sun_sep(az: float, alt: float, sun: tuple[float, float]) -> float:
    return angular_sep_deg(az, alt, sun[1], sun[0])


def axis0_target(current: float, az: float, mech_min: float, mech_max: float) -> float | None:
    """Axis-0 position PWI4 ends at for target azimuth `az`: the representation (az + 360k)
    nearest to `current` among those inside the mechanical range (when the nearest one is out
    of range PWI4 takes the one that fits: measured on the sim). None if none fits."""
    best: float | None = None
    k0 = math.floor((mech_min - az) / 360.0)
    for k in range(k0, k0 + 4):
        r = az + 360.0 * k
        if mech_min <= r <= mech_max and (best is None or abs(r - current) < abs(best - current)):
            best = r
    return best


def simulate_axis(
    x0: float,
    x1: float,
    vmax: float,
    accel: float,
    tau: float,
    sample_dt: float = PATH_SAMPLE_S,
    dt: float = SIM_DT_S,
    t_max: float = PATH_T_MAX_S,
) -> list[float]:
    """Positions every `sample_dt` seconds (first entry = start) for a PWI4 axis slew.

    Model (fitted on recorded sim slews, see BUILD_PLAN / T3.1b log): commanded speed is
    clamp(distance / tau, vmax); the actual speed follows it limited by `accel`; the axis
    stops when it is within ARRIVE_DEG. tau is the slew time constant (P13).
    """
    out = [x0]
    x, v, t = x0, 0.0, 0.0
    steps_per_sample = max(1, round(sample_dt / dt))
    n = 0
    while t < t_max:
        d = x1 - x
        if abs(d) < ARRIVE_DEG and abs(v) < 0.05:
            break
        vc = max(-vmax, min(vmax, d / max(tau, 1e-3)))
        step = accel * dt
        v += max(-step, min(step, vc - v))
        x += v * dt
        t += dt
        n += 1
        if n % steps_per_sample == 0:
            out.append(x)
    out.append(x1)
    return out


@dataclass(frozen=True)
class AxisParams:
    vmax: float
    accel: float


def predict_paths(
    start: tuple[float, float],
    target: tuple[float, float],
    ax0: AxisParams,
    ax1: AxisParams,
    tau: float = SLEW_TAU_DEFAULT_S,
    sample_dt: float = PATH_SAMPLE_S,
) -> list[list[tuple[float, float]]]:
    """Predicted (axis0, axis1) tracks of a goto, sampled every `sample_dt`.

    Two timing variants are returned: with the accelerations PWI4 reports, and with no
    acceleration limit. On the sim the axes reach full speed almost instantly (the reported
    20 deg/s^2 is not what the sim does: rms error 4.8 deg with it vs 0.25 deg without), while
    a real mount obeys it. The checker refuses a slew if EITHER variant crosses the Sun, so a
    wrong acceleration figure can not hide a crossing. Axes move independently.
    """
    paths = []
    for scale in (1.0, None):
        a0 = ax0.accel if scale else INSTANT_ACCEL
        a1 = ax1.accel if scale else INSTANT_ACCEL
        p0 = simulate_axis(start[0], target[0], ax0.vmax, a0, tau, sample_dt)
        p1 = simulate_axis(start[1], target[1], ax1.vmax, a1, tau, sample_dt)
        n = max(len(p0), len(p1))
        p0 += [p0[-1]] * (n - len(p0))
        p1 += [p1[-1]] * (n - len(p1))
        paths.append(list(zip(p0, p1, strict=True)))
    return paths


def path_min_sun_sep(
    paths: list[list[tuple[float, float]]], sun: tuple[float, float]
) -> tuple[float, float]:
    """(minimum Sun separation over all samples of all variants, separation at the start).
    Axis 0 is azimuth, axis 1 altitude (alt-az mount)."""
    best = 360.0
    for path in paths:
        for a0, a1 in path:
            best = min(best, sun_sep(a0 % 360.0, a1, sun))
    a0, a1 = paths[0][0]
    return best, sun_sep(a0 % 360.0, a1, sun)


def corridor_distance(
    pos: tuple[float, float], paths: list[list[tuple[float, float]]], sky: bool = False
) -> float:
    """Smallest axis-space distance (deg) from `pos` to any predicted path (polyline through the
    0.1 s samples) of either timing variant.

    `sky` (object tracks, Q19): axis-0 offsets are scaled by cos(the mount's altitude), so the
    distance is the pointing offset on the sky. Near the zenith the azimuth axis lags at its max
    rate (P22) by many axis degrees that are only ~1 deg on the sky."""
    px, py = pos
    if sky:
        k = max(math.cos(math.radians(py)), 0.0)
        px *= k
        paths = [[(x * k, y) for x, y in path] for path in paths]
    best = math.inf
    for path in paths:
        if len(path) == 1:
            best = min(best, math.hypot(px - path[0][0], py - path[0][1]))
        for (x0, y0), (x1, y1) in zip(path, path[1:], strict=False):
            dx, dy = x1 - x0, y1 - y0
            seg = dx * dx + dy * dy
            u = 0.0 if seg < 1e-12 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg))
            best = min(best, math.hypot(px - (x0 + u * dx), py - (y0 + u * dy)))
    return best


@dataclass
class ValidatedGoto:
    """A goto the gateway itself checked: target inside the limits, path clear of the Sun."""

    start: tuple[float, float]  # axis0, axis1 at validation time
    target: tuple[float, float]  # axis0, axis1 goal
    paths: list[list[tuple[float, float]]]
    duration_s: float  # longest predicted variant
    start_sun_sep: float | None = None  # None: Sun checks do not apply
    t_begin: float = 0.0
    last_move_t: float = 0.0
    # T3.5c: a D-SUN route plan. `paths` = [the route corridor]. It ends only at
    # t_begin + duration_s or by clear(): the mount legitimately waits (LEAD) and holds at the end,
    # and the predictive Sun rule must not fire on the route's own pass at 16 deg.
    route: bool = False
    # T4.2 Q19: object tracks measure the corridor on the sky (see corridor_distance)
    sky: bool = False


@dataclass
class RoutedGoto:
    """A goto whose predicted path crosses the Sun disc, planned by D-SUN (stored per command id
    like a ValidatedGoto; the executor takes it)."""

    route: Any  # dsun.Route
    start: tuple[float, float]  # axis0, axis1 at validation time
    target: tuple[float, float]  # axis0, axis1 goal
    start_sun_sep: float | None = None


def actual_limit_decision(
    alt: float,
    az: float,
    lim_el: float,
    sun: tuple[float, float] | None,
    floor_alt: float,
    start_sun_sep: float | None,
) -> tuple[str, dict[str, float]] | None:
    """Actual-state limits shared by validated gotos and HOME (no extrapolation):
    - altitude more than LIMIT_MARGIN_DEG below min(Lim EL, start altitude);
    - Sun separation <= 15 deg (or deeper than at the start when it started inside).
    """
    floor = min(lim_el, floor_alt)
    if alt < floor - LIMIT_MARGIN_DEG:
        return r_below(lim_el), {"alt": alt, "floor": floor}
    if sun is not None:
        sep = sun_sep(az, alt, sun)
        limit = (
            KEEPOUT_DEG
            if start_sun_sep is None or start_sun_sep > KEEPOUT_DEG
            else start_sun_sep - 0.05
        )
        if sep <= limit:
            return R_SUN, {"sun_sep": sep}
    return None


@dataclass
class HomingWatch:
    """While HOME runs the real path is unknown (P12): only the ACTUAL limits are watched."""

    start_alt: float
    start_sun_sep: float | None = None  # None: Sun checks do not apply (shortcut / no Sun)


def validated_decision(
    alt: float,
    axes: tuple[float, float],
    plan: ValidatedGoto,
    lim_el: float,
    sun: tuple[float, float] | None,
    az: float,
    corridor_tol: float = CORRIDOR_TOL_DEG,
) -> tuple[str, dict[str, float]] | None:
    """Monitor rule while a validated goto is in progress: no linear extrapolation (a legal
    slew toward a limit would be cut short). Uses the ACTUAL state only:
    - altitude more than LIMIT_MARGIN_DEG below min(Lim EL, start altitude) (the second term
      keeps a recovery slew that starts below Lim EL from stopping at once);
    - actual Sun separation <= 15 deg (or deeper than the start when it started inside);
    - position further than `corridor_tol` from the validated predicted path.
    """
    hit = actual_limit_decision(alt, az, lim_el, sun, plan.start[1], plan.start_sun_sep)
    if hit is not None:
        return hit
    dev = corridor_distance(axes, plan.paths, plan.sky)
    if dev > corridor_tol:
        return R_CORRIDOR, {"deviation_deg": dev, "tolerance_deg": corridor_tol}
    return None


def monitor_decision(
    alt: float,
    az: float,
    v_alt: float,
    v_az: float,
    lim_el: float,
    sun: tuple[float, float] | None,
    lookahead: float = LOOKAHEAD_S,
) -> tuple[str, dict[str, float]] | None:
    """Predictive limit check for the 10 Hz monitor. Returns (reason, details) or None.

    - Lim EL: stop when alt + v_alt * lookahead < lim_el while descending. Inside the limit,
      motion that is not descending (recovery) is left alone.
    - Sun: stop when the predicted separation < 15 deg and the separation is closing.
      `sun is None` means the shortcut applies / no Sun data: Sun checks skipped.
    """
    if v_alt < -NEGLIGIBLE_V and alt + v_alt * lookahead < lim_el:
        return r_below(lim_el), {"alt": alt, "v_alt": v_alt, "pred_alt": alt + v_alt * lookahead}
    if sun is not None:
        now = sun_sep(az, alt, sun)
        pred = sun_sep(az + v_az * lookahead, alt + v_alt * lookahead, sun)
        if pred < KEEPOUT_DEG and pred < now - 1e-6:
            return R_SUN, {"sun_sep": now, "pred_sun_sep": pred}
    return None


@dataclass
class StallEvent:
    axis: int
    dist_arcsec: float
    duration_s: float


@dataclass
class _AxisStall:
    ref_pos: float | None = None
    t0: float = 0.0


class StallWatch:
    """Per-axis stall detector (pure; fed one /status sample at a time, no I/O)."""

    def __init__(self) -> None:
        self._axes = [_AxisStall(), _AxisStall()]

    def reset(self) -> None:
        self._axes = [_AxisStall(), _AxisStall()]

    @staticmethod
    def commanded_dist_arcsec(ax: Any) -> float | None:
        """Distance to the target when the axis is commanded to move, else None."""
        dist = ax.dist_to_target_arcsec
        if (
            dist is None
            and ax.target_mech_position_degs is not None
            and ax.position_degs is not None
        ):
            dist = (ax.target_mech_position_degs - ax.position_degs) * 3600.0
        sv = getattr(ax, "setpoint_velocity_degs_per_sec", None)
        if dist is not None and abs(dist) > STALL_CMD_DIST_ARCSEC:
            return abs(dist)
        if sv is not None and abs(sv) > STALL_CMD_SETPOINT_V:
            return abs(dist) if dist is not None else 0.0
        return None

    def update(
        self, t: float, mount: Any, active: bool, jog_axis: int | None = None
    ) -> StallEvent | None:
        """`active` = the mount is slewing / tracking (and not in a sequence). Returns the first
        stalled axis, or None. The reference restarts whenever the axis moves or is not
        commanded. `jog_axis`: the axis a running jog drives (T3.4c): it counts as commanded
        whatever its reported target distance, so a jog into a mechanical limit trips the guard."""
        hit: StallEvent | None = None
        for i, ax in enumerate((mount.axis0, mount.axis1)):
            st = self._axes[i]
            pos = ax.position_degs
            dist = self.commanded_dist_arcsec(ax) if active and pos is not None else None
            if dist is None and active and pos is not None and jog_axis == i:
                dist = abs(ax.dist_to_target_arcsec or 0.0)
            if dist is None:
                st.ref_pos = None
                continue
            if st.ref_pos is None or abs(pos - st.ref_pos) * 3600.0 > STALL_POS_TOL_ARCSEC:
                st.ref_pos, st.t0 = pos, t
                continue
            if t - st.t0 >= STALL_S and hit is None:
                hit = StallEvent(i, dist, t - st.t0)
        return hit


def parse_sun_override(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    try:
        alt, az = (float(x) for x in text.split(","))
    except ValueError:
        return None
    return alt, az


# ------------------------------------------------------------------ engine


class SafetyEngine:
    def __init__(
        self,
        poller: Poller,
        store: SettingsStore | None,
        ephem: Ephem,
        audit: AuditLog,
        sun_override: tuple[float, float] | None = None,
    ) -> None:
        self.poller = poller
        self.store = store
        self.ephem = ephem
        self.audit = audit
        self.sun_override = sun_override
        self._sun_cache: tuple[float, tuple[float, float, float], tuple[float, float]] | None = None
        self._target_az: dict[str, float] = {}
        self._plans: dict[str, ValidatedGoto] = {}
        self._routes: dict[str, RoutedGoto] = {}
        # T3.5c / Q10: HomedGuard.state ("homed" | "overridden" | "not_homed" | None). A HOME is
        # routed around the Sun only when a home COMPLETED in this connect session.
        self.homed_state: Any = lambda: None
        if sun_override is not None:
            audit.write("test_sun_override", alt=sun_override[0], az=sun_override[1])

    @classmethod
    def from_env(cls, poller, store, ephem, audit) -> SafetyEngine:
        return cls(
            poller, store, ephem, audit, parse_sun_override(os.environ.get("PW_TEST_SUN_ALTAZ"))
        )

    # -- inputs

    def lim_el(self) -> float:
        if self.store is not None:
            return float(self.store.current.hardware.lim_el_deg)
        return 15.0

    @staticmethod
    def pwi4_site(status: Any) -> tuple[float, float, float] | None:
        s = status.site
        if s.latitude_degs is None or s.longitude_degs is None:
            return None
        return (s.latitude_degs, s.longitude_degs, s.height_meters or 0.0)

    def sun_altaz(self, site: tuple[float, float, float]) -> tuple[float, float] | None:
        """Sun (alt, az) at PWI4's site, cached for SUN_CACHE_S (the Sun moves 0.004 deg/s)."""
        if self.sun_override is not None:
            return self.sun_override
        now = time.monotonic()
        c = self._sun_cache
        if c is not None and c[1] == site and now - c[0] < SUN_CACHE_S:
            return c[2]
        if not self.ephem.available:
            return None
        pos = self.ephem.sun_altaz(site)
        self._sun_cache = (now, site, pos)
        return pos

    def sun_altaz_at(
        self, site: tuple[float, float, float], t_unix: float
    ) -> tuple[float, float] | None:
        """Sun (alt, az) at an arbitrary Unix time (T4.2b track planner; uncached)."""
        if self.sun_override is not None:
            return self.sun_override
        if not self.ephem.available:
            return None
        from datetime import UTC, datetime

        return self.ephem.sun_altaz(site, datetime.fromtimestamp(t_unix, UTC))

    def home_pose(self) -> tuple[float, float]:
        """(alt, az) of the home pose used for the Sun path check (settings, hand-editable)."""
        if self.store is not None:
            hw = self.store.current.hardware
            return float(hw.home_alt_deg), float(hw.home_az_deg)
        return DEFAULT_HOME_ALT, DEFAULT_HOME_AZ

    def check_home(self, st: Any) -> HomeVerdict:
        """HOME gate (T3.2). Refuses when the site differs from PWI4's, the home pose is below
        Lim EL or inside the Sun disc, or the predicted path (both predictor variants) from the
        current pose to the home pose passes within 15 deg of the Sun. The Sun shortcut and the
        recovery rule are the same as for gotos. Home is blocked until D-SUN (T3.5) detours it.
        """
        site = self.pwi4_site(st)
        m = st.mount
        if site is None or m.altitude_degs is None or m.azimuth_degs is None:
            return HomeVerdict("PWI4 not running / not reachable")
        if self.store is not None:
            sc = compute_site_check(self.store.current.location, site)
            if sc["ok"] is False:
                return HomeVerdict(R_SITE)
        lim = self.lim_el()
        h_alt, h_az = self.home_pose()
        if h_alt < lim:
            return HomeVerdict(f"Home elevation {h_alt:g}° is below the station limit ({lim:g}°)")
        sun = self.sun_altaz(site)
        use_sun = sun is not None and not sun_shortcut(sun[0], lim, m.altitude_degs)
        if use_sun and sun_sep(h_az, h_alt, sun) <= KEEPOUT_DEG:
            return HomeVerdict(R_SUN, sun_blocked=True)
        a0, a1 = m.axis0, m.axis1
        if None in (
            a0.position_degs,
            a1.position_degs,
            a0.min_mech_position_degs,
            a0.max_mech_position_degs,
        ):
            return HomeVerdict("Mount position unavailable (cannot verify the home path)")
        tgt0 = axis0_target(
            a0.position_degs, h_az, a0.min_mech_position_degs, a0.max_mech_position_degs
        )
        if tgt0 is None:
            return HomeVerdict(R_NO_AZ)
        start_sep = None
        if use_sun:
            tau = getattr(m, "slew_time_constant", None) or SLEW_TAU_DEFAULT_S
            p0 = AxisParams(
                a0.max_velocity_degs_per_sec or 20.0, a0.acceleration_degs_per_sec_sqr or 20.0
            )
            p1 = AxisParams(
                a1.max_velocity_degs_per_sec or 20.0, a1.acceleration_degs_per_sec_sqr or 20.0
            )
            paths = predict_paths((a0.position_degs, a1.position_degs), (tgt0, h_alt), p0, p1, tau)
            low, start_sep = path_min_sun_sep(paths, sun)
            limit = KEEPOUT_DEG if start_sep > KEEPOUT_DEG else start_sep - 0.05
            if low <= limit:
                blocked = HomeVerdict(R_PATH_SUN, sun_blocked=True, start_sun_sep=start_sep)
                # Q10: detour only with a pose that can be trusted, i.e. a home COMPLETED in
                # this connect session ("overridden" / stalled / not homed do not count)
                if self.homed_state() != "homed":
                    return blocked
                from . import dsun

                res = self.plan_detour(
                    (a0.position_degs, a1.position_degs), (tgt0, h_alt), sun, lim, a0, a1
                )
                if isinstance(res, dsun.NoRoute):
                    return blocked
                return HomeVerdict(None, False, start_sep, route=res)
        return HomeVerdict(None, False, start_sep)

    # -- command gate (ControlService.check)

    def take_plan(self, cmd: Command) -> ValidatedGoto | None:
        return self._plans.pop(cmd.id, None)

    def take_route(self, cmd: Command) -> RoutedGoto | None:
        return self._routes.pop(cmd.id, None)

    @staticmethod
    def _axis_params(a0: Any, a1: Any) -> tuple[AxisParams, AxisParams]:
        return (
            AxisParams(
                a0.max_velocity_degs_per_sec or 20.0, a0.acceleration_degs_per_sec_sqr or 20.0
            ),
            AxisParams(
                a1.max_velocity_degs_per_sec or 20.0, a1.acceleration_degs_per_sec_sqr or 20.0
            ),
        )

    def plan_detour(
        self,
        start: tuple[float, float],
        target: tuple[float, float],
        sun: tuple[float, float],
        lim: float,
        a0: Any,
        a1: Any,
    ) -> Any:
        """D-SUN route for a slew the predictor flagged as crossing the disc: a Route, or a
        dsun.NoRoute. When the planner straight axis-space line is clear (it returns None) the
        per-axis dog-leg of the predictor is what crosses: fly the straight line as a route
        (force=True), NEVER the plain goto."""
        from . import dsun

        p0, p1 = self._axis_params(a0, a1)
        lo, hi = a0.min_mech_position_degs, a0.max_mech_position_degs
        rng = (lo if lo is not None else -1e9, hi if hi is not None else 1e9)
        max1 = a1.max_mech_position_degs if a1.max_mech_position_degs is not None else 90.0
        res = dsun.plan_route(start, target, sun, lim, p0, p1, rng, max1)
        if res is None:
            res = dsun.plan_route(start, target, sun, lim, p0, p1, rng, max1, force=True)
        return res

    @staticmethod
    def detour_info(route: Any) -> dict[str, Any]:
        from . import dsun

        return {
            "duration_s": round(route.duration_s + dsun.LEAD_S + ROUTE_SETTLE_S, 1),
            "side": ",".join(route.sides) if route.sides else "straight",
        }

    def final_check(self, st: Any, cmd: Command) -> str | None:
        """The short final goto after a route (and the rest of a truncated wrap_turn sweep):
        judged like a goto from the ACTUAL pose, with the same predictor and recovery rule.
        Stores the validated plan for `goto_started` (the monitor)."""
        site = self.pwi4_site(st) if st is not None else None
        m = st.mount if st is not None else None
        if site is None or m is None or m.altitude_degs is None or m.azimuth_degs is None:
            return "PWI4 not running / not reachable"
        a0, a1 = m.axis0, m.axis1
        if None in (a0.position_degs, a1.position_degs):
            return "Mount position unavailable (cannot verify the final move)"
        lim = self.lim_el()
        if cmd.name in ("goto_altaz", "wrap_turn"):
            t_alt, t_az = cmd.args["alt"], cmd.args["az"]
        else:
            if not self.ephem.available:
                return R_NO_EPHEM
            t_alt, t_az = self.ephem.radec_j2000_altaz(site, cmd.args["ra_h"], cmd.args["dec_deg"])
        if t_alt < lim:
            return r_below(lim)
        if "target0" in cmd.args:  # wrap_turn, or the track rendezvous goto (T4.2c)
            tgt0 = cmd.args["target0"]
        elif None not in (a0.min_mech_position_degs, a0.max_mech_position_degs):
            tgt0 = axis0_target(
                a0.position_degs, t_az, a0.min_mech_position_degs, a0.max_mech_position_degs
            )
            if tgt0 is None:
                return R_NO_AZ
        else:
            return None
        sun = self.sun_altaz(site)
        use_sun = sun is not None and not sun_shortcut(sun[0], lim, m.altitude_degs)
        tau = getattr(m, "slew_time_constant", None) or SLEW_TAU_DEFAULT_S
        p0, p1 = self._axis_params(a0, a1)
        start = (a0.position_degs, a1.position_degs)
        paths = predict_paths(start, (tgt0, t_alt), p0, p1, tau)
        start_sep = None
        if use_sun:
            if sun_sep(t_az, t_alt, sun) <= KEEPOUT_DEG:
                return R_SUN
            low, start_sep = path_min_sun_sep(paths, sun)
            if low <= (KEEPOUT_DEG if start_sep > KEEPOUT_DEG else start_sep - 0.05):
                return R_PATH_SUN
        self._plans[cmd.id] = ValidatedGoto(
            start=start,
            target=(tgt0, t_alt),
            paths=paths,
            duration_s=max(len(p) for p in paths) * PATH_SAMPLE_S,
            start_sun_sep=start_sep,
        )
        if len(self._plans) > 32:
            self._plans.pop(next(iter(self._plans)))
        return None

    def pop_target_az(self, cmd: Command) -> float | None:
        return self._target_az.pop(cmd.id, None)

    def check(self, cmd: Command, ctx: CommandContext) -> str | None:
        name = cmd.name
        if name == "track_object":  # T4.1: only the site gate here; the pass rules are trackcheck
            sc = ctx.site_check
            if sc is not None and sc.ok is False:
                ctx.code = reason_code(R_SITE)
                return R_SITE
            return None
        if name not in ("goto_altaz", "goto_radec_j2000", "jog", "step", "wrap_turn") and not (
            name == "tracking" and cmd.args.get("on")
        ):
            return None  # stop / locks / tracking off: never gated here
        reason = self._check_motion(cmd, ctx)
        if reason:
            ctx.code = reason_code(reason)
        if reason and not ctx.dry:  # a dry run (POST /api/mount/check) leaves no trace
            self.audit.write(
                "safety_refusal", client=ctx.conn_id, cmd=name, args=cmd.args, reason=reason
            )
        return reason

    def _check_jog(
        self,
        cmd: Command,
        ctx: CommandContext,
        m: Any,
        lim: float,
        sun: tuple[float, float] | None,
    ) -> str | None:
        """Jog (and a step while TRACKING; a step from STOPPED is judged as a goto): refuse a
        direction that leads straight into Lim EL or the Sun disc at the current pose, i.e. when
        the mount could not be stopped in time (`jog_margin_deg`) or the step itself would end
        inside. Moving away from a limit (recovery) is always allowed. The mount's own travel
        limits and the wrap window are the gateway's (JogController.refusal)."""
        axis, d = cmd.args["axis"], cmd.args["dir"]
        alt, az = m.altitude_degs, m.azimuth_degs
        if cmd.name == "jog":
            ax = (m.axis0, m.axis1)[axis]
            margin = jog_margin_deg(cmd.args["speed"], ax.acceleration_degs_per_sec_sqr or 20.0)
        else:
            margin = cmd.args["size_arcmin"] / 60.0
        d_alt = d * margin if axis == 1 else 0.0
        d_az = d * margin if axis == 0 else 0.0
        if cmd.name == "step":
            ctx.target = (alt + d_alt, (az + d_az) % 360.0)
        if d_alt < 0.0 and alt + d_alt < lim:
            return r_below(lim)
        if sun is not None:
            now = sun_sep(az, alt, sun)
            n = 8
            for k in range(1, n + 1):
                f = k / n
                sep = sun_sep(az + d_az * f, alt + d_alt * f, sun)
                if sep <= KEEPOUT_DEG and sep < now - 1e-6:
                    return R_SUN
        return None

    def _route_goto(
        self,
        cmd: Command,
        ctx: CommandContext,
        start: tuple[float, float],
        target: tuple[float, float],
        sun: tuple[float, float],
        lim: float,
        a0: Any,
        a1: Any,
        start_sep: float | None,
    ) -> str | None:
        """The predictor flagged the path (T3.5): detour planner instead of R_PATH_SUN. Returns
        None (allowed, route stored / reported) or the refusal reason."""
        from . import dsun

        res = self.plan_detour(start, target, sun, lim, a0, a1)
        if isinstance(res, dsun.NoRoute):
            # start / target inside the disc: the recovery / target rules keep the old refusal
            return R_NO_DETOUR if res.reason == "no_detour" else R_PATH_SUN
        ctx.code = "sun_detour"
        ctx.detour = self.detour_info(res)
        if not ctx.dry:
            self._routes[cmd.id] = RoutedGoto(res, start, target, start_sep)
            if len(self._routes) > 32:
                self._routes.pop(next(iter(self._routes)))
        return None

    def _check_motion(self, cmd: Command, ctx: CommandContext) -> str | None:
        sc = ctx.site_check
        if sc is not None and sc.ok is False:
            return R_SITE
        st = ctx.status
        if st is None:
            return "PWI4 not running / not reachable"
        site = self.pwi4_site(st)
        m = st.mount
        if site is None or m.altitude_degs is None or m.azimuth_degs is None:
            return "PWI4 not running / not reachable"
        lim = self.lim_el()
        cur_alt, cur_az = m.altitude_degs, m.azimuth_degs
        sun = self.sun_altaz(site)
        use_sun = sun is not None and not sun_shortcut(sun[0], lim, cur_alt)

        if cmd.name == "tracking":
            if cur_alt < lim:
                return r_below(lim)
            if use_sun and sun_sep(cur_az, cur_alt, sun) <= KEEPOUT_DEG:
                return R_SUN
            return None

        if cmd.name in ("jog", "step"):
            return self._check_jog(cmd, ctx, m, lim, sun if use_sun else None)

        if cmd.name == "wrap_turn" and "target0" not in cmd.args:
            return "PWI4 not running / not reachable"  # no turn plan (the status changed)
        if cmd.name in ("goto_altaz", "wrap_turn"):
            t_alt, t_az = cmd.args["alt"], cmd.args["az"]
        else:
            if not self.ephem.available:
                return R_NO_EPHEM
            t_alt, t_az = self.ephem.radec_j2000_altaz(site, cmd.args["ra_h"], cmd.args["dec_deg"])
        ctx.target = (t_alt, t_az)
        if t_alt < lim:
            return r_below(lim)
        if use_sun and sun_sep(t_az, t_alt, sun) <= KEEPOUT_DEG:
            return R_SUN

        a0, a1 = m.axis0, m.axis1
        if None in (a0.position_degs, a1.position_degs):
            return None
        tgt0 = None
        if cmd.name == "wrap_turn" or (cmd.name == "goto_altaz" and "target0" in cmd.args):
            # T3.4e: the effective command carries the axis-0 target (the OTHER representation of
            # the current azimuth). Judged exactly like a goto: Lim EL / Sun target above, the
            # Sun PATH (the sweep along the whole altitude circle) below.
            # T4.2c: the track rendezvous goto carries the planner's axis-0 representation too.
            tgt0 = cmd.args["target0"]
        elif None not in (a0.min_mech_position_degs, a0.max_mech_position_degs):
            tgt0 = axis0_target(
                a0.position_degs, t_az, a0.min_mech_position_degs, a0.max_mech_position_degs
            )
            if tgt0 is None:
                return R_NO_AZ
        if cmd.name == "goto_radec_j2000" and not ctx.dry:
            self._target_az[cmd.id] = t_az
            if len(self._target_az) > 32:
                self._target_az.pop(next(iter(self._target_az)))
        if tgt0 is not None:
            tau = getattr(m, "slew_time_constant", None) or SLEW_TAU_DEFAULT_S
            p0 = AxisParams(
                a0.max_velocity_degs_per_sec or 20.0, a0.acceleration_degs_per_sec_sqr or 20.0
            )
            p1 = AxisParams(
                a1.max_velocity_degs_per_sec or 20.0, a1.acceleration_degs_per_sec_sqr or 20.0
            )
            start = (a0.position_degs, a1.position_degs)
            paths = predict_paths(start, (tgt0, t_alt), p0, p1, tau)
            start_sep = None
            if use_sun:
                low, start_sep = path_min_sun_sep(paths, sun)
                # Recovery: starting inside the disc is allowed when the path never gets closer
                # to the Sun than where the mount already is.
                limit = KEEPOUT_DEG if start_sep > KEEPOUT_DEG else start_sep - 0.05
                if low <= limit:
                    return self._route_goto(
                        cmd, ctx, start, (tgt0, t_alt), sun, lim, a0, a1, start_sep
                    )
            if not ctx.dry:
                self._plans[cmd.id] = ValidatedGoto(
                    start=start,
                    target=(tgt0, t_alt),
                    paths=paths,
                    duration_s=max(len(p) for p in paths) * PATH_SAMPLE_S,
                    start_sun_sep=start_sep,
                )
                if len(self._plans) > 32:
                    self._plans.pop(next(iter(self._plans)))
        return None


# ------------------------------------------------------------------ monitor


class SafetyMonitor:
    """10 Hz predictive limit monitor. Runs in every mode, also for motion started in PWI4."""

    def __init__(self, engine: SafetyEngine, poller: Poller, control: ControlService) -> None:
        self.engine = engine
        self.poller = poller
        self.control = control
        self._hist: deque[tuple[float, float, float, float, float]] = deque(maxlen=64)
        self._last_t = -1.0
        self._last_stop = -1e9
        self.stops = 0
        self.plan: ValidatedGoto | None = None
        self.homing: HomingWatch | None = None
        self.stall = StallWatch()
        self.stall_event: StallEvent | None = None  # set by tick(), consumed by _run()
        self.stalls = 0
        # T3.4c: (axis, dir, speed) of the running jog, or None (JogController.probe).
        self.jog_probe: Any = lambda: None
        self._stall_t = -1.0
        self._last_stall_stop = -1e9
        self._task: asyncio.Task | None = None

    def begin_homing(self, verdict: HomeVerdict) -> None:
        """HOME is about to run: watch the actual limits only (the real path is unknown)."""
        st = self.control.fresh_status()
        alt = st.mount.altitude_degs if st is not None else None
        self.homing = HomingWatch(start_alt=alt if alt is not None else 0.0)
        self.homing.start_sun_sep = verdict.start_sun_sep

    def end_homing(self) -> None:
        self.homing = None

    def begin(self, plan: ValidatedGoto) -> None:
        """A goto the gateway validated has been sent: switch to the actual-state rules."""
        plan.t_begin = plan.last_move_t = time.monotonic()
        self.plan = plan

    def begin_route(
        self,
        corridor: list[tuple[float, float]],
        target: tuple[float, float],
        duration_s: float,
        start_sun_sep: float | None,
        start: tuple[float, float],
        sky: bool = False,
    ) -> None:
        """A D-SUN route is about to be uploaded: watch the actual limits and the route corridor
        until t_begin + duration_s (LEAD + route + settle), regardless of idle / arrival.
        `sky`: an object track (corridor measured on the sky, Q19)."""
        self.begin(
            ValidatedGoto(
                start=start,
                target=target,
                paths=[corridor],
                duration_s=duration_s,
                start_sun_sep=start_sun_sep,
                route=True,
                sky=sky,
            )
        )

    def extend_route(
        self, corridor: list[tuple[float, float]], target: tuple[float, float], extra_s: float
    ) -> None:
        """A long track got its next chunk (T4.2c): the running route plan grows with it."""
        p = self.plan
        if p is None or not p.route:
            return
        p.paths[0].extend(corridor)
        p.target = target
        p.duration_s += extra_s

    def clear(self) -> None:
        """New command / STOP / link loss: back to the predictive rules."""
        self.plan = None
        self.homing = None
        self.stall.reset()  # a new command / STOP gets a fresh stall window

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def velocity(self) -> tuple[float, float] | None:
        """(v_alt, v_az) from the newest sample vs one at least VEL_WINDOW_S older.

        v_az is the azimuth AXIS rate (axis-0 position, continuous) when reported: the reported
        azimuth flips by 180 deg when axis 1 passes the zenith (the sim allows 91 deg), and that
        spike once inflated the speed-dependent look-ahead to ~16 s and fired a false Lim EL stop
        at alt 87 (Phase 3 end, test_stall_pwi4sim). Fallback: the wrapped azimuth difference."""
        if len(self._hist) < 2:
            return None
        t, alt, az, ax0, _ = self._hist[-1]
        for t0, alt0, az0, ax00, _ in reversed(self._hist):
            if t - t0 >= VEL_WINDOW_S:
                dt = t - t0
                if math.isfinite(ax0) and math.isfinite(ax00):
                    daz = ax0 - ax00
                else:
                    daz = (az - az0 + 180.0) % 360.0 - 180.0
                return (alt - alt0) / dt, daz / dt
        return None

    def _sample(self) -> tuple[Any, float] | None:
        st = self.control.fresh_status()
        if st is None or not st.mount.is_connected:
            self._hist.clear()
            return None
        m = st.mount
        if m.altitude_degs is None or m.azimuth_degs is None:
            return None
        t = self.poller.last_good_t
        if t != self._last_t:
            self._last_t = t
            ax0 = m.axis0.position_degs
            self._hist.append(
                (t, m.altitude_degs, m.azimuth_degs, math.nan if ax0 is None else ax0, 0.0)
            )
        return st, t

    def _check_stall(self, st: Any, t: float) -> None:
        """Stall guard (T3.3b): fed once per new /status sample; sets `stall_event`.
        Not evaluated during a HOME run (its path and fields are unknown, P12) or a connect /
        disconnect sequence."""
        if t == self._stall_t:
            return
        self._stall_t = t
        m = st.mount
        active = (
            bool(m.is_slewing or m.is_tracking)
            and self.homing is None
            and not self.control.seq.active
            and bool(m.axis0.is_enabled and m.axis1.is_enabled)
        )
        jog = self.jog_probe()
        ev = self.stall.update(t, m, active, jog[0] if jog is not None else None)
        if ev is not None:
            self.stall_event = ev

    def tick(self) -> tuple[str, dict[str, float]] | None:
        """Evaluate once; returns the stop decision (None = fine). No I/O."""
        got = self._sample()
        if got is None:
            return None
        st, t_sample = got
        self._check_stall(st, t_sample)
        vel = self.velocity()
        if vel is None:
            return None
        m = st.mount
        eng = self.engine
        lim = eng.lim_el()
        site = eng.pwi4_site(st)
        sun = eng.sun_altaz(site) if site is not None else None
        if sun is not None and sun_shortcut(sun[0], lim, m.altitude_degs):
            sun = None
        watch = self.homing
        if watch is not None:  # HOME: actual limits only, no corridor / no extrapolation
            return actual_limit_decision(
                m.altitude_degs, m.azimuth_degs, lim, sun, watch.start_alt, watch.start_sun_sep
            )
        plan = self.plan
        if plan is not None:
            axes = (m.axis0.position_degs, m.axis1.position_degs)
            if None in axes:
                return None
            now = time.monotonic()
            if math.hypot(vel[0], vel[1]) > 0.05:
                plan.last_move_t = now
            if plan.route:  # a route plan ends by the clock (or clear()) only
                done = now - plan.t_begin > plan.duration_s
            else:
                dist = math.hypot(axes[0] - plan.target[0], axes[1] - plan.target[1])
                idle = now - plan.last_move_t > PLAN_STILL_S and now - plan.t_begin > 1.0
                expired = now - plan.t_begin > plan.duration_s * 1.5 + 10.0
                # Arrived = close AND settled (Phase 3 end): ending the plan while the mount still
                # closes in on a target just above Lim EL handed it to the predictive rule with a
                # lagging velocity, which could stop a legal goto to Lim EL + 0.1 in its last step.
                settled = math.hypot(vel[0], vel[1]) <= 0.05
                done = (dist < ARRIVED_DEG and settled) or idle or expired
            if done:
                self.plan = None  # ends; the predictive rules take over (tracking, drift)
            else:
                return validated_decision(m.altitude_degs, axes, plan, lim, sun, m.azimuth_degs)
        v_alt, v_az = vel
        jog = self.jog_probe()
        if jog is not None:  # a running jog: judge by its commanded rate while the axis ramps up
            ax, d, speed = jog
            if ax == 1:
                v_alt = d * max(speed, abs(v_alt))
            else:
                v_az = d * max(speed, abs(v_az))
        fast = m.axis1 if abs(v_alt) >= abs(v_az) else m.axis0
        look = lookahead_s(max(abs(v_alt), abs(v_az)), fast.acceleration_degs_per_sec_sqr or 20.0)
        return monitor_decision(m.altitude_degs, m.azimuth_degs, v_alt, v_az, lim, sun, look)

    async def _run(self) -> None:
        while True:
            try:
                decision = self.tick()
                now = time.monotonic()
                ev, self.stall_event = self.stall_event, None
                if ev is not None and now - self._last_stall_stop >= STOP_REPEAT_S:
                    self._last_stall_stop = now
                    self.stalls += 1
                    self.stall.reset()
                    await self.control.stall_stop(ev.axis, ev.dist_arcsec, ev.duration_s)
                if decision and now - self._last_stop >= STOP_REPEAT_S:
                    self._last_stop = now
                    self.stops += 1
                    reason, details = decision
                    await self.control.safety_stop(
                        reason, **{k: round(v, 4) for k, v in details.items()}
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # the monitor must never die
                pass
            await asyncio.sleep(MONITOR_PERIOD_S)
