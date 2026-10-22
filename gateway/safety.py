"""Safety engine v1 (T3.1b): target checks, slew-path prediction, Sun shortcut, 10 Hz monitor.

Plugs into the T3.1a seams: `ControlService.check` (command gate) and `SafetyMonitor`
(continuous predictive stop through `ControlService.safety_stop`).

All alt/az and Sun maths use PWI4's own site (from /status), never the Settings location
(O-SITE). Pure functions live at the top so T3.2 (HOME) and T3.5 (D-SUN) can reuse them.

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
from .control import CommandContext, ControlService
from .ephem import Ephem, angular_sep_deg
from .poller import Poller
from .settings import SUN_KEEPOUT_DEG, SettingsStore

KEEPOUT_DEG = SUN_KEEPOUT_DEG  # 15 deg
LOOKAHEAD_S = 0.5  # monitor look-ahead on the measured velocity
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

R_SITE = "Station location differs from PWI4's site"
R_SUN = "Within 15° of the Sun"
R_PATH_SUN = "Slew path passes within 15° of the Sun"
R_NO_EPHEM = "Ephemeris unavailable (cannot verify target)"
R_NO_AZ = "Target azimuth is outside the mount's mechanical range"
R_CORRIDOR = "Mount left the validated slew path"


def r_below(lim_el: float) -> str:
    return f"Below station limit elevation (Lim EL {lim_el:g}°)"


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


def corridor_distance(pos: tuple[float, float], paths: list[list[tuple[float, float]]]) -> float:
    """Smallest axis-space distance (deg) from `pos` to any predicted path (polyline through the
    0.1 s samples) of either timing variant."""
    px, py = pos
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
    floor = min(lim_el, plan.start[1])
    if alt < floor - LIMIT_MARGIN_DEG:
        return r_below(lim_el), {"alt": alt, "floor": floor}
    if sun is not None:
        sep = sun_sep(az, alt, sun)
        ss = plan.start_sun_sep
        limit = KEEPOUT_DEG if ss is None or ss > KEEPOUT_DEG else ss - 0.05
        if sep <= limit:
            return R_SUN, {"sun_sep": sep}
    dev = corridor_distance(axes, plan.paths)
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

    # -- command gate (ControlService.check)

    def take_plan(self, cmd: Command) -> ValidatedGoto | None:
        return self._plans.pop(cmd.id, None)

    def pop_target_az(self, cmd: Command) -> float | None:
        return self._target_az.pop(cmd.id, None)

    def check(self, cmd: Command, ctx: CommandContext) -> str | None:
        name = cmd.name
        if name not in ("goto_altaz", "goto_radec_j2000") and not (
            name == "tracking" and cmd.args.get("on")
        ):
            return None  # stop / locks / tracking off: never gated here
        reason = self._check_motion(cmd, ctx)
        if reason:
            self.audit.write(
                "safety_refusal", client=ctx.conn_id, cmd=name, args=cmd.args, reason=reason
            )
        return reason

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

        if cmd.name == "goto_altaz":
            t_alt, t_az = cmd.args["alt"], cmd.args["az"]
        else:
            if not self.ephem.available:
                return R_NO_EPHEM
            t_alt, t_az = self.ephem.radec_j2000_altaz(site, cmd.args["ra_h"], cmd.args["dec_deg"])
        if t_alt < lim:
            return r_below(lim)
        if use_sun and sun_sep(t_az, t_alt, sun) <= KEEPOUT_DEG:
            return R_SUN

        a0, a1 = m.axis0, m.axis1
        if None in (a0.position_degs, a1.position_degs):
            return None
        tgt0 = None
        if None not in (a0.min_mech_position_degs, a0.max_mech_position_degs):
            tgt0 = axis0_target(
                a0.position_degs, t_az, a0.min_mech_position_degs, a0.max_mech_position_degs
            )
            if tgt0 is None:
                return R_NO_AZ
        if cmd.name == "goto_radec_j2000":
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
        self._task: asyncio.Task | None = None

    def begin(self, plan: ValidatedGoto) -> None:
        """A goto the gateway validated has been sent: switch to the actual-state rules."""
        plan.t_begin = plan.last_move_t = time.monotonic()
        self.plan = plan

    def clear(self) -> None:
        """New command / STOP / link loss: back to the predictive rules."""
        self.plan = None

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
        """(v_alt, v_az) from the newest sample vs one at least VEL_WINDOW_S older."""
        if len(self._hist) < 2:
            return None
        t, alt, az, _, _ = self._hist[-1]
        for t0, alt0, az0, _, _ in reversed(self._hist):
            if t - t0 >= VEL_WINDOW_S:
                dt = t - t0
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
            self._hist.append((t, m.altitude_degs, m.azimuth_degs, 0.0, 0.0))
        return st, t

    def tick(self) -> tuple[str, dict[str, float]] | None:
        """Evaluate once; returns the stop decision (None = fine). No I/O."""
        got = self._sample()
        if got is None:
            return None
        st, _ = got
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
        plan = self.plan
        if plan is not None:
            axes = (m.axis0.position_degs, m.axis1.position_degs)
            if None in axes:
                return None
            now = time.monotonic()
            if math.hypot(vel[0], vel[1]) > 0.05:
                plan.last_move_t = now
            dist = math.hypot(axes[0] - plan.target[0], axes[1] - plan.target[1])
            idle = now - plan.last_move_t > PLAN_STILL_S and now - plan.t_begin > 1.0
            expired = now - plan.t_begin > plan.duration_s * 1.5 + 10.0
            if dist < ARRIVED_DEG or idle or expired:
                self.plan = None  # ends; the predictive rules take over (tracking, drift)
            else:
                return validated_decision(m.altitude_degs, axes, plan, lim, sun, m.azimuth_degs)
        return monitor_decision(m.altitude_degs, m.azimuth_degs, vel[0], vel[1], lim, sun)

    async def _run(self) -> None:
        while True:
            try:
                decision = self.tick()
                now = time.monotonic()
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
