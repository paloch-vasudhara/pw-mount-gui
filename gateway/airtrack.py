"""Aircraft cueing (T5.6b): TRACK AIRCRAFT from the verdict to "Aircraft below Lim EL".

Spec: BUILD_PLAN Phase 5b "T5.6b" and finding P23 (a running custom alt/az path can be replaced
every 1 s without stops or jumps). Two parts:

* `AirChecker` (the verdict, shared by POST /api/mount/check and the real command through
  `ControlService.air_verdict`): the aircraft rules in order, the straight-line overflight
  prediction (D3 zenith rule), and the rendezvous plan.
* `AirTrackExecutor` (one per gateway, `ControlService.airx`): the rendezvous goto, then every 1 s
  a new path from the runner's aircraft record (`adsb_core.predict.path`, from now - 0.5 s to
  now + 10 s, 0.5 s points, look angles from PWI4's site, refracted like PWI4, D1 / D2),
  uploaded as new + add_point_list + apply(update_wrap=0). The absolute point times carry the
  latency compensation: the fix time drives the prediction, the points are timed in the present.

Phases (`AirTrackInfo.phase`):

  slewing   the rendezvous goto (an ordinary validated goto_altaz job with `target0` and the
            clamped `wrap_min`, D-SUN routed when the Sun is in the way) -> arrival (within 0.3 deg,
            still). Late: one re-plan, late again -> "Missed the aircraft".
  waiting   path uploaded, its first point is t_r: the mount holds until then (P21).
  tracking  from t_r; the first point of every path is now - 0.5 s (a point in the future makes the
            mount hold still until it, P23).

The wrap window is set ONCE, before the rendezvous goto, and never moved (P9 / P23); a path point
outside it is a cut. A path is cut before the first point below Lim EL, within 16 deg of the Sun,
outside the window, beyond the range limit or needing more than RATE_FRACTION of axis 0's vmax; the
track then ends 1 s after its last point, with that reason. The loop never relies on the path hold:
it stops the mount itself, also when it crashes (`_abort`).

Timers use `ControlService.air_clock` (Unix seconds) and `air_sleep` (test seams).
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import dsun, state
from .adsb import SITE_HAE_OFFSET_M
from .commands import Command
from .control import (
    R_LINK_CANCELLED,
    R_NO_MOUNT,
    R_NO_PWI4,
    R_STOP_CANCELLED,
    ROUTE_WRAP_FIT_DEG,
    ROUTE_WRAP_MARGIN_DEG,
    Pwi4Error,
    _Job,
)
from .safety import KEEPOUT_DEG, sun_sep
from .snapshot import AirTrackEndedInfo, AirTrackInfo
from .trackplan import ZENITH_NOTE_ALT_DEG, TrackMount, mount_from_status, slew_estimate_s
from .transforms import refract_alt

if TYPE_CHECKING:
    from .control import ControlService

AIRCRAFT_REFRACT = True  # D2: the path is sent refracted (PWI4 works in observed alt/az)

# the check (Q44 / Q46)
MAX_FIX_AGE_S = 3.0  # a fix older than this is refused ("ADS-B position too old")
CROSS_WINDOW_S = 60.0  # below Lim EL now: it may cross within this
OVERFLIGHT_S = 180.0  # the straight-line overflight prediction for the zenith rule
OVERFLIGHT_STEP_S = 1.0
RDV_MARGIN_S = 3.0
RDV_ITERATIONS = 3
RDV_MAX_S = 60.0  # a rendezvous further away than this is refused
RDV_CROSS_PAD_S = 0.5
RDV_CROSS_PAD_DEG = 0.5
SITE_NOTE_M = 30.0  # D1: Settings vs PWI4 site differing more than this adds a note

# the executor
UPDATE_S = 1.0  # a new path every second (P23: not faster)
HORIZON_S = 10.0
STEP_S = 0.5
LEAD_BACK_S = 0.5  # the first point is never in the future
LOST_AFTER_S = 10.0  # Q60: the last fix older than this ends the track
KALMAN_MAX_S = 5.0  # Q60: on the Kalman fallback this long ends the track
END_DELAY_S = 1.0  # settle after the last path point before the stop
MONITOR_SETTLE_S = 3.0  # the monitor's route plan outlives the path by this
ARRIVE_TOL_DEG = 0.3
ARRIVE_POLL_S = 0.2
REPLAN_GOTO_DEG = 0.5
ENDED_TTL_S = 30.0

R_NOT_AVAILABLE = "ADS-B not available"
R_NOT_IN_VIEW = "Aircraft not in view"
R_STALE_FIX = "ADS-B position too old"
R_OUT_OF_RANGE = "Out of range"
R_BELOW = "Below Lim EL"
R_SUN_NOW = "Within 15° of the Sun"
R_ZENITH = "Overflight too close to the zenith"
R_NO_RDV = "Cannot reach the aircraft in time"
R_WRAP_FIT = "Track does not fit the wrap window"
R_ZENITH_NOTE = "Near-zenith overflight: tracking lags near the peak"
# end reasons (path cuts and feed conditions)
R_END_BELOW = "Aircraft below Lim EL"
R_END_SUN = "Sun approaching"
R_END_WRAP = "Track left the wrap window"
R_END_RANGE = "Out of range"
R_END_ZENITH = "Overflight too close to the zenith"
R_DATA_LOST = "ADS-B data lost"
R_PREDICTION = "ADS-B prediction unreliable"
R_LOST = "Aircraft lost"
R_MISSED = "Missed the aircraft"

CODE_BY_REASON = {
    R_NOT_AVAILABLE: "adsb_down",
    R_NOT_IN_VIEW: "not_in_view",
    R_STALE_FIX: "stale_fix",
    R_OUT_OF_RANGE: "out_of_range",
    R_BELOW: "below_limit",
    R_SUN_NOW: "sun",
    R_ZENITH: "zenith",
    R_NO_RDV: "no_rendezvous",
    R_WRAP_FIT: "no_rendezvous",
}
# a path that cannot start at the rendezvous: the check's reason for each cut
CHECK_REASON_OF_CUT = {
    R_END_BELOW: R_BELOW,
    R_END_SUN: R_SUN_NOW,
    R_END_WRAP: R_WRAP_FIT,
    R_END_RANGE: R_OUT_OF_RANGE,
    R_END_ZENITH: R_ZENITH,
    R_PREDICTION: R_NOT_IN_VIEW,
    R_LOST: R_NOT_IN_VIEW,
}
# cuts that end the track as "done" (the path ran out); the rest are stops
PATH_ENDS = frozenset({R_END_BELOW, R_END_SUN, R_END_WRAP, R_END_RANGE, R_END_ZENITH})


def _jd(t_unix: float) -> float:
    return t_unix / 86400.0 + 2440587.5


# ------------------------------------------------------------------ geometry


@dataclass(frozen=True)
class AirPoint:
    t: float  # Unix seconds
    az: float  # 0..360, PWI4's observed frame (refracted, D2)
    alt: float
    axis0: float  # continuous axis-0 position inside the wrap window


@dataclass
class AirCtx:
    """Everything the path maths needs (one per update / per check)."""

    core: Any  # gateway.adsb.Core
    site: Any  # adsb_core Geodetic: PWI4's site, HAE (D1)
    lim_el: float
    sun: tuple[float, float] | None  # (alt, az) now
    range_m: float
    vmax0: float  # axis 0 vmax (deg/s)
    mech: tuple[float, float]  # axis 0 mechanical range


def look(ctx: AirCtx, pred: Any) -> tuple[float, float, float] | None:
    """(observed alt, az, slant range m) of a predicted position; None without a height."""
    if pred is None or pred.height_m is None:
        return None
    g = ctx.core.geometry
    lk = g.look_angles(ctx.site, g.Geodetic(pred.lat, pred.lon, pred.height_m))
    alt = refract_alt(lk.el) if AIRCRAFT_REFRACT else lk.el
    return alt, lk.az, lk.range


def axis0_in(az: float, wrap_min: float) -> float:
    """The axis-0 value of azimuth `az` inside the window [wrap_min, wrap_min + 360)."""
    return wrap_min + (az - wrap_min) % 360.0


def window_for(rdv_axis0: float, mech: tuple[float, float]) -> float:
    """wrap_min centred on the rendezvous axis 0, clamped so [wrap_min, wrap_min + 360] stays inside
    the mechanical range when that range is at least 360 deg wide (§2)."""
    w = rdv_axis0 - 180.0
    if mech[1] - mech[0] >= 360.0:
        w = min(max(w, mech[0]), mech[1] - 360.0)
    return w


def axis0_candidates(current: float, az: float, mech: tuple[float, float]) -> list[float]:
    """The axis-0 positions (az + 360 k) inside the mechanical range, nearest to `current` first."""
    k0 = math.floor((mech[0] - az) / 360.0)
    reps = [az + 360.0 * k for k in range(k0, k0 + 4) if mech[0] <= az + 360.0 * k <= mech[1]]
    return sorted(reps, key=lambda r: abs(r - current))


@dataclass
class AirPath:
    points: list[AirPoint]
    reason: str | None = None  # why the path stops short (None: it runs to the horizon)


def build_path(ctx: AirCtx, ac: Any, t0: float, t1: float, wrap_min: float) -> AirPath:
    """The predicted path from t0 to t1 as PWI4 alt/az points, cut before the first violation
    (below Lim EL, within R_DETOUR_DEG of the Sun, outside the wrap window or the mechanical
    range, beyond the range limit, or a required axis-0 rate above RATE_FRACTION * vmax)."""
    preds = ctx.core.predict.path(ac, t0, max(t1, t0), STEP_S)
    if not preds:
        return AirPath([], R_PREDICTION)
    lo, hi = wrap_min + ROUTE_WRAP_MARGIN_DEG, wrap_min + ROUTE_WRAP_FIT_DEG + 1.0
    max_rate = dsun.RATE_FRACTION * ctx.vmax0
    pts: list[AirPoint] = []
    for t, p in preds:
        lk = look(ctx, p)
        if lk is None:
            return AirPath(pts, R_LOST)
        alt, az, rng = lk
        reason = None
        ax0 = axis0_in(az, wrap_min)
        if alt < ctx.lim_el:
            reason = R_END_BELOW
        elif ctx.sun is not None and sun_sep(az, alt, ctx.sun) < dsun.R_DETOUR_DEG:
            reason = R_END_SUN
        elif not (lo <= ax0 <= hi and ctx.mech[0] <= ax0 <= ctx.mech[1]):
            reason = R_END_WRAP
        elif rng > ctx.range_m:
            reason = R_END_RANGE
        elif pts:
            d_ax0 = abs(ax0 - pts[-1].axis0)
            if d_ax0 > 180.0:  # the azimuth ran across the window edge
                reason = R_END_WRAP
            elif d_ax0 / (t - pts[-1].t) > max_rate:
                reason = R_END_ZENITH
        if reason is not None:
            return AirPath(pts, reason)
        pts.append(AirPoint(t, az, alt, ax0))
    return AirPath(pts)


# ------------------------------------------------------------------ the verdict


@dataclass
class AirPlan:
    """What the executor flies: the rendezvous and the wrap window."""

    icao: str
    callsign: str | None
    site: tuple[float, float, float]  # PWI4's site (D1)
    t_r: float
    rendezvous: AirPoint
    wrap_min: float


@dataclass
class AirCheck:
    """What the check endpoint reports for `track_aircraft`: `reason` None = allowed."""

    reason: str | None
    code: str | None = None
    target: tuple[float, float] | None = None  # the aircraft's observed (alt, az) now
    notes: list[str] = field(default_factory=list)
    aircraft: dict[str, Any] | None = None  # {icao, callsign, peak_el, t_peak}
    plan: AirPlan | None = None


class AirChecker:
    """The TRACK AIRCRAFT verdict (rules 1-8 of T5.6b §1). Pure maths on a copy of the runner's
    aircraft record; the Sun, Lim EL, the clock and the runner come in as callables."""

    def __init__(
        self,
        runner: Callable[[], Any],
        lim_el: Callable[[], float],
        sun_at: Callable[[tuple[float, float, float], float], tuple[float, float] | None],
        clock: Callable[[], float] = time.time,
        settings_site: Callable[[], tuple[float, float, float] | None] = lambda: None,
    ) -> None:
        self.runner = runner
        self.lim_el = lim_el
        self.sun_at = sun_at
        self.clock = clock
        self.settings_site = settings_site

    def context(
        self, runner: Any, site: tuple[float, float, float], mount: TrackMount, now: float
    ) -> AirCtx:
        core = runner.core
        return AirCtx(
            core,
            core.geometry.Geodetic(site[0], site[1], site[2] + SITE_HAE_OFFSET_M),
            self.lim_el(),
            self.sun_at(site, now),
            runner.range_limit_m,
            mount.ax0.vmax,
            mount.axis0_range,
        )

    def evaluate(self, icao: str, site: tuple[float, float, float], st: Any) -> AirCheck:
        runner = self.runner()
        if runner is None or getattr(runner, "core", None) is None:
            return AirCheck(R_NOT_AVAILABLE, "adsb_down")
        if not runner.green:
            return AirCheck(runner.detail(), "adsb_down")
        raw = runner.aircraft_for(icao)
        if (
            raw is None
            or raw.fix_t is None
            or raw.lat is None
            or raw.lon is None
            or raw.height_m is None
        ):
            return AirCheck(R_NOT_IN_VIEW, "not_in_view")
        mount = mount_from_status(st) if st is not None else None
        if mount is None:
            return AirCheck(R_NO_PWI4, "no_pwi4")
        ac = copy.copy(raw)  # the record is updated from the event loop
        now = self.clock()
        ctx = self.context(runner, site, mount, now)
        notes: list[str] = []
        info: dict[str, Any] = {
            "icao": ac.icao, "callsign": ac.callsign, "peak_el": None, "t_peak": None,
        }  # fmt: skip

        def refuse(reason: str, target: tuple[float, float] | None = None) -> AirCheck:
            return AirCheck(reason, CODE_BY_REASON[reason], target, notes, info)

        s = self.settings_site()
        if s is not None:  # D1: the display keeps the Settings location, the pointing uses PWI4's
            e, n, _ = ctx.core.geometry.to_enu(
                ctx.core.geometry.Geodetic(site[0], site[1], 0.0),
                ctx.core.geometry.Geodetic(s[0], s[1], 0.0),
            )
            d = math.hypot(e, n)
            if d > SITE_NOTE_M:
                notes.append(f"Settings location differs from PWI4's site by {round(d)} m")

        pos = look(ctx, ctx.core.predict.position_at(ac, now))
        if pos is None:
            return refuse(R_NOT_IN_VIEW)
        alt, az, rng = pos
        target = (alt, az)
        if now - ac.fix_t > MAX_FIX_AGE_S:
            return refuse(R_STALE_FIX, target)
        if rng > ctx.range_m:
            return refuse(R_OUT_OF_RANGE, target)

        # a straight-line (velocity projection) copy for the look-ahead rules
        line = copy.copy(ac)
        line.turn_rate_dps = None
        t_cross = None
        if alt < ctx.lim_el:
            t_cross = self._crossing(ctx, line, now, ctx.lim_el)
            if t_cross is None:
                return refuse(R_BELOW, target)
        if ctx.sun is not None and sun_sep(az, alt, ctx.sun) <= KEEPOUT_DEG:
            return refuse(R_SUN_NOW, target)

        rate, peak = self._overflight(ctx, line, now)
        if peak is not None:
            info["peak_el"], info["t_peak"] = round(peak[1], 2), round(peak[0], 1)
        if rate > dsun.RATE_FRACTION * ctx.vmax0:
            return refuse(R_ZENITH, target)
        if peak is not None and peak[1] >= ZENITH_NOTE_ALT_DEG:
            notes.append(R_ZENITH_NOTE)

        res = self._rendezvous(ctx, ac, line, mount, now, t_cross)
        if isinstance(res, str):
            return refuse(res, target)
        rdv, wrap_min = res
        plan = AirPlan(ac.icao, ac.callsign, site, rdv.t, rdv, wrap_min)
        return AirCheck(None, None, target, notes, info, plan)

    # -- rules

    @staticmethod
    def _crossing(ctx: AirCtx, line: Any, now: float, alt_min: float) -> float | None:
        """First time within CROSS_WINDOW_S the (straight-line) observed altitude is >= alt_min."""
        t = now
        while t <= now + CROSS_WINDOW_S:
            lk = look(ctx, ctx.core.predict.position_at(line, t))
            if lk is not None and lk[0] >= alt_min:
                return t
            t += STEP_S
        return None

    @staticmethod
    def _overflight(ctx: AirCtx, line: Any, now: float) -> tuple[float, tuple[float, float] | None]:
        """(the largest axis-0 rate (deg/s) needed while above Lim EL, (t, alt) of the peak) over
        the next OVERFLIGHT_S on the straight line."""
        worst, peak = 0.0, None
        prev = None
        t = now
        while t <= now + OVERFLIGHT_S:
            lk = look(ctx, ctx.core.predict.position_at(line, t))
            if lk is None:
                break
            alt, az, _ = lk
            if alt >= ctx.lim_el:
                if peak is None or alt > peak[1]:
                    peak = (t, alt)
                if prev is not None:
                    d_az = abs((az - prev + 180.0) % 360.0 - 180.0)
                    worst = max(worst, d_az / OVERFLIGHT_STEP_S)
                prev = az
            else:
                prev = None
            t += OVERFLIGHT_STEP_S
        return worst, peak

    def _rendezvous(
        self,
        ctx: AirCtx,
        ac: Any,
        line: Any,
        mount: TrackMount,
        now: float,
        t_cross: float | None,
    ) -> tuple[AirPoint, float] | str:
        """The rendezvous like T4.2: t_r = now + slew estimate + 3 s, iterated, aiming at the
        PREDICTED position at t_r; a rising aircraft waits for Lim EL + 0.5 deg. Returns
        (rendezvous point, wrap_min) or the refusal reason."""
        predict = ctx.core.predict
        t_r = now + RDV_MARGIN_S
        for _ in range(RDV_ITERATIONS):
            lk = look(ctx, predict.position_at(ac, t_r))
            if lk is None:
                return R_NOT_IN_VIEW
            t_r = now + slew_estimate_s(mount, lk[1], lk[0]) + RDV_MARGIN_S
        if t_cross is not None:  # below Lim EL now: wait for the crossing (+ pad)
            t_up = self._crossing(ctx, line, now, ctx.lim_el + RDV_CROSS_PAD_DEG)
            if t_up is None:
                return R_BELOW
            t_r = max(t_r, t_up + RDV_CROSS_PAD_S)
        if t_r - now > RDV_MAX_S:
            return R_NO_RDV
        lk = look(ctx, predict.position_at(ac, t_r))
        if lk is None:
            return R_NOT_IN_VIEW
        alt, az, rng = lk
        if alt < ctx.lim_el:
            return R_BELOW
        if rng > ctx.range_m:
            return R_OUT_OF_RANGE
        # The axis-0 representation of the rendezvous azimuth: the one nearest to the mount unless
        # another keeps the (straight-line) track inside its wrap window for longer.
        best: tuple[int, float, float, AirPath] | None = None
        for tgt0 in axis0_candidates(mount.axis0, az, ctx.mech):
            w = window_for(tgt0, ctx.mech)
            path = build_path(ctx, line, t_r, t_r + OVERFLIGHT_S, w)
            if best is None or len(path.points) > best[0]:
                best = (len(path.points), tgt0, w, path)
        if best is None:
            return R_WRAP_FIT
        n, tgt0, wrap_min, path = best
        if n < 2:
            return CHECK_REASON_OF_CUT.get(path.reason or "", R_WRAP_FIT)
        return AirPoint(t_r, az, alt, tgt0), wrap_min


# ------------------------------------------------------------------ the executor


class _Air:
    """One aircraft track (mutable; owned by the executor)."""

    def __init__(self, conn_id: str, cmd: Command, ac: AirCheck, job: _Job) -> None:
        plan = ac.plan
        assert plan is not None
        self.conn_id = conn_id
        self.cmd = cmd
        self.icao: str = plan.icao
        self.callsign: str | None = plan.callsign
        self.name = self.callsign or self.icao
        self.plan = plan
        self.t_r = plan.t_r
        self.wrap_min = plan.wrap_min
        self.notes = list(ac.notes)
        self.job: _Job | None = job
        self.task: asyncio.Task | None = None
        self.phase = "slewing"
        self.t0 = time.monotonic()
        self.override_text: str | None = None
        self.replanned = False
        self.started = False  # the first path went out
        self.kalman_since: float | None = None
        self.cut: tuple[float, str] | None = None  # (time of the last kept point, reason)
        self.mount: TrackMount | None = None
        self.updates = 0


class AirTrackExecutor:
    def __init__(self, svc: ControlService) -> None:
        self.svc = svc
        self.a: _Air | None = None
        self.ended: AirTrackEndedInfo | None = None
        self._ended_override = False

    # ------------------------------------------------------------------ public state

    @property
    def active(self) -> bool:
        return self.a is not None

    @property
    def slewing(self) -> bool:
        return self.a is not None and self.a.phase == "slewing"

    def info(self) -> AirTrackInfo | None:
        """Snapshot `airtrack` (poller provider): null when idle; only `ended` for ENDED_TTL_S
        after an end."""
        ended = self.ended
        if ended is not None and self.svc.air_clock() - ended.at > ENDED_TTL_S:
            self.ended = ended = None
        a = self.a
        if a is None:
            return AirTrackInfo(ended=ended) if ended is not None else None
        return AirTrackInfo(
            icao=a.icao, callsign=a.callsign, phase=a.phase, t_r=a.t_r, notes=list(a.notes),
            ended=ended,
        )  # fmt: skip

    # ------------------------------------------------------------------ start / end

    def begin(self, conn_id: str, cmd: Command, ac: AirCheck, job: _Job) -> None:
        """The command was accepted (verdict ok, rendezvous goto queued): run the track."""
        svc = self.svc
        a = _Air(conn_id, cmd, ac, job)
        self.a = a
        self.ended = None
        svc.audit.write(
            "track_aircraft", client=conn_id, id=cmd.id, icao=a.icao, callsign=a.callsign,
            t_r=round(a.t_r, 3),
        )  # fmt: skip
        a.task = svc._spawn(self._main(a))
        svc.hub.republish_latest()

    def on_cancel(self, reason: str) -> None:
        """STOP / link loss / safety stop / stall / shutdown (`ControlService._cancel_all`)."""
        if self.a is None:
            return
        if reason == R_STOP_CANCELLED:
            text = "Stopped by STOP"
        elif reason == R_LINK_CANCELLED:
            text = "Control link lost"
        else:
            text = reason.removeprefix("Cancelled: ")
        self.end("track_stopped", text)

    def end(self, event: str, reason: str, *, quiet: bool = False) -> bool:
        """End the running aircraft track NOW (sync): records, task, rendezvous job. The caller
        stops the mount (STOP, the replacing command, the sequence). `event` track_stopped shows
        in the snapshot's `ended`; track_replaced does not. True when a track was running."""
        a = self.a
        if a is None:
            return False
        self._close(
            a, "airtrack_" + event.removeprefix("track_"), reason,
            ended=(event == "track_stopped" and not quiet), quiet=quiet,
        )  # fmt: skip
        if a.task is not None and not a.task.done() and a.task is not asyncio.current_task():
            a.task.cancel()
        job = a.job
        if job is not None and not job.finished.is_set():
            job.cancelled_reason = reason
            if job.task is not None and not job.task.done():
                job.task.cancel()
            if not job.future.done():
                job.future.set_result((False, reason))
        return True

    def _close(
        self, a: _Air, event: str, reason: str, *, ended: bool, quiet: bool = False, **fields: Any
    ) -> bool:
        if self.a is not a:
            return False
        svc = self.svc
        self.a = None
        svc.motion_cleared()  # the monitor's route plan ends with the track
        ov = state.get_override()
        if ov is not None and ov[0] == "TRACKING":
            state.clear_override()
        if not quiet:
            svc.audit.write(
                event, client=a.conn_id, id=a.cmd.id, icao=a.icao, callsign=a.callsign,
                reason=reason, phase=a.phase, elapsed_s=round(time.monotonic() - a.t0, 1), **fields,
            )  # fmt: skip
        if ended:
            self.ended = AirTrackEndedInfo(icao=a.icao, reason=reason, at=svc.air_clock())
        svc.hub.republish_latest()
        return True

    def clear_ended_override(self) -> None:
        """The next command clears the "Aircraft below Lim EL" etc. state reason."""
        if not self._ended_override:
            return
        self._ended_override = False
        ov = state.get_override()
        if ov is not None and ov[0] == "STOPPED":
            state.clear_override()
            self.svc.hub.republish_latest()

    # ------------------------------------------------------------------ the task

    async def _sleep(self, seconds: float) -> None:
        await self.svc.air_sleep(max(0.0, seconds))

    async def _main(self, a: _Air) -> None:
        try:
            await self._flow(a)
        except asyncio.CancelledError:
            raise
        except Pwi4Error as exc:
            await self._stop_track(a, "airtrack_stopped", str(exc))
        except Exception as exc:  # a bug must still leave the mount safe and say so
            self.svc.audit.write("airtrack_error", icao=a.icao, error=repr(exc))
            await self._stop_track(a, "airtrack_stopped", f"Track failed ({type(exc).__name__})")

    async def _stop_track(self, a: _Air, event: str, reason: str, **fields: Any) -> None:
        """The track is over (or cannot go on): close it as STOPPED with `reason`, stop the mount"""
        svc = self.svc
        if self.a is not a:
            return
        self._close(a, event, reason, ended=True, **fields)
        err = None
        try:
            await svc._stop_requests()
            await svc.writer.tracking_off()
        except Pwi4Error as exc:
            err = str(exc)
        svc.motion_cleared()
        if err is not None and self.ended is not None:
            reason = f"{reason}; STOP not delivered: {err}"
            self.ended = AirTrackEndedInfo(icao=a.icao, reason=reason, at=self.ended.at)
        state.set_override("STOPPED", reason)
        self._ended_override = True
        svc.hub.republish_latest()
        svc._spawn(svc._confirm_after_stop(a.conn_id, a.cmd.id))

    async def _flow(self, a: _Air) -> None:
        # 1. the rendezvous goto (queued by ControlService._air_command)
        await self._await_job(a)
        if self.a is not a:
            return
        if not await self._arrive(a):
            if not await self._replan(a):
                return
        # 2. the paths, replaced every second
        await self._follow(a)

    async def _await_job(self, a: _Air) -> None:
        job = a.job
        if job is None:
            return
        await job.finished.wait()
        if self.a is a and job.error:
            raise Pwi4Error(job.error)

    # ------------------------------------------------------------------ rendezvous

    def _pose(self) -> tuple[float, float, Any] | None:
        st = self.svc.fresh_status()
        if st is None:
            return None
        m = st.mount
        if m.axis0.position_degs is None or m.axis1.position_degs is None:
            return None
        return m.axis0.position_degs, m.axis1.position_degs, st

    async def _arrive(self, a: _Air) -> bool:
        """Wait until the mount sits at the rendezvous. False when it is too late for t_r."""
        svc = self.svc
        rdv = a.plan.rendezvous
        while True:
            pose = self._pose()
            if pose is not None:
                if not pose[2].mount.is_connected:
                    raise Pwi4Error(R_NO_MOUNT)
                still = not svc.poller.tracker.moving_at(time.monotonic())
                if (
                    still
                    and abs(pose[0] - rdv.axis0) <= ARRIVE_TOL_DEG
                    and abs(pose[1] - rdv.alt) <= ARRIVE_TOL_DEG
                ):
                    return svc.air_clock() <= a.t_r - dsun.LEAD_S
            if svc.air_clock() > a.t_r - dsun.LEAD_S:
                return False
            await self._sleep(ARRIVE_POLL_S)

    async def _replan(self, a: _Air) -> bool:
        """The mount was late: plan the rendezvous again from now (once). True = go on."""
        svc = self.svc
        if a.replanned:
            await self._stop_track(a, "airtrack_stopped", R_MISSED)
            return False
        a.replanned = True
        site = svc.poller.pwi4_site()
        pose = self._pose()
        if site is None or pose is None:
            await self._stop_track(a, "airtrack_stopped", R_NO_PWI4)
            return False
        res = svc.air_eval(a.icao, site, pose[2])
        if res.reason or res.plan is None:
            await self._stop_track(a, "airtrack_stopped", res.reason or R_MISSED)
            return False
        old_t_r = a.t_r
        a.plan, a.t_r = res.plan, res.plan.t_r
        a.notes = list(res.notes)
        rdv = res.plan.rendezvous
        dist = math.hypot(pose[0] - rdv.axis0, pose[1] - rdv.alt)
        svc.audit.write(
            "airtrack_replan", icao=a.icao, old_t_r=round(old_t_r, 3), t_r=round(a.t_r, 3),
            move_deg=round(dist, 3),
        )  # fmt: skip
        svc.hub.republish_latest()
        if dist <= REPLAN_GOTO_DEG:
            return True  # the window of the first goto stays (it is never moved afterwards)
        goto = Command(
            f"{a.cmd.id}:rdv2", "goto_altaz",
            {
                "alt": rdv.alt, "az": rdv.az % 360.0, "target0": rdv.axis0,
                "wrap_min": res.plan.wrap_min,
            },
        )  # fmt: skip
        v = svc._motion_verdict(goto, a.conn_id)
        if v.reason:
            await self._stop_track(a, "airtrack_stopped", v.reason)
            return False
        job = _Job(goto, a.conn_id)
        try:
            svc._queue.put_nowait(job)
        except asyncio.QueueFull:
            await self._stop_track(a, "airtrack_stopped", "Command queue full")
            return False
        a.job, a.wrap_min = job, res.plan.wrap_min
        ok, reason = await job.future
        if not ok:
            if self.a is a:
                await self._stop_track(a, "airtrack_stopped", reason or "Command failed")
            return False
        await self._await_job(a)
        if self.a is not a:
            return False
        if not await self._arrive(a):
            await self._stop_track(a, "airtrack_stopped", R_MISSED)
            return False
        return True

    # ------------------------------------------------------------------ following

    def _set_phase(self, a: _Air, now: float) -> None:
        """Phase + the TRACKING state override from the clock."""
        if now < a.t_r:
            phase, text = "waiting", f"Waiting for {a.name}"
        else:
            phase, text = "tracking", f"Tracking {a.name}"
        if phase == a.phase and text == a.override_text:
            return
        a.phase, a.override_text = phase, text
        state.set_override("TRACKING", text)
        self.svc.hub.republish_latest()

    def _feed_reason(self, a: _Air, now: float) -> str | None:
        """Q60 and the connection: the reason to end the track now, or None."""
        runner = self.svc.adsb_runner
        core = getattr(runner, "core", None)
        if runner is None or core is None:
            return R_NOT_AVAILABLE
        if not runner.green:
            return runner.detail()
        ac = runner.aircraft_for(a.icao)
        if ac is None:
            return R_LOST
        if ac.fix_t is None or now - ac.fix_t > LOST_AFTER_S:
            return R_DATA_LOST
        pred = core.predict.position_at(ac, now)
        if pred is None:
            return R_PREDICTION
        if pred.method == "kalman":
            if a.kalman_since is None:
                a.kalman_since = now
            elif now - a.kalman_since > KALMAN_MAX_S:
                return R_PREDICTION
        else:
            a.kalman_since = None
        return None

    async def _follow(self, a: _Air) -> None:
        svc = self.svc
        nxt = svc.air_clock()
        while self.a is a:
            now = svc.air_clock()
            pose = self._pose()
            if pose is not None and not pose[2].mount.is_connected:
                await self._stop_track(a, "airtrack_stopped", R_NO_MOUNT)
                return
            why = self._feed_reason(a, now)
            if why is not None:
                await self._stop_track(a, "airtrack_stopped", why)
                return
            if a.cut is not None and now >= a.cut[0] + END_DELAY_S:
                await self._stop_track(
                    a, "airtrack_done" if a.cut[1] in PATH_ENDS else "airtrack_stopped", a.cut[1]
                )
                return
            self._set_phase(a, now)
            if not await self._update(a, now, pose):
                return
            nxt += UPDATE_S
            if nxt < svc.air_clock() - UPDATE_S:  # fell behind (a slow PWI4): do not burst
                nxt = svc.air_clock()
            await self._sleep(nxt - svc.air_clock())

    async def _update(self, a: _Air, now: float, pose: tuple[float, float, Any] | None) -> bool:
        """One 1 s step: the path from the runner's record, cut, uploaded, monitored. False = the
        track ended here."""
        svc = self.svc
        runner = svc.adsb_runner
        checker = svc.air_checker
        ac = runner.aircraft_for(a.icao)
        if ac is None or checker is None:
            await self._stop_track(a, "airtrack_stopped", R_LOST)
            return False
        mount = mount_from_status(pose[2]) if pose is not None else a.mount
        if mount is None:
            await self._stop_track(a, "airtrack_stopped", R_NO_PWI4)
            return False
        a.mount = mount
        ctx = checker.context(runner, a.plan.site, mount, now)
        ac = copy.copy(ac)
        waiting = now < a.t_r
        t0 = a.t_r if waiting else now - LEAD_BACK_S
        path = build_path(ctx, ac, t0, max(now + HORIZON_S, t0 + STEP_S), a.wrap_min)
        pts = path.points
        if len(pts) < 2:
            reason = path.reason or R_PREDICTION
            await self._stop_track(
                a, "airtrack_done" if reason in PATH_ENDS else "airtrack_stopped", reason
            )
            return False
        a.cut = (pts[-1].t, path.reason) if path.reason is not None else None
        last = pts[-1]
        corridor = [(p.axis0, p.alt) for p in pts]
        total = max(0.0, last.t - now) + END_DELAY_S + MONITOR_SETTLE_S
        if not a.started:
            svc.motion_cleared()
            await svc.writer.tracking_off()
            # a STEP from sidereal TRACKING keeps its axis offset (jog.py); it must not shift the
            # track: reset now that the mount no longer tracks
            await svc.writer.mount_offset(axis0_reset=1, axis1_reset=1)
            start = (pose[0], pose[1]) if pose is not None else (mount.axis0, mount.axis1)
            svc.route_begin(corridor, (last.axis0, last.alt), total, None, start, sky=True)
        elif not svc.route_replace(corridor, (last.axis0, last.alt), total):
            svc.route_begin(  # the plan was cleared meanwhile: watch the new path again
                corridor, (last.axis0, last.alt), total, None, (mount.axis0, mount.axis1), sky=True
            )
        await svc.writer.custom_path_new("altaz")
        await svc.writer.custom_path_add_points([(_jd(p.t), p.az, p.alt) for p in pts])
        await svc.writer.custom_path_apply(0)  # update_wrap=0 always (P8)
        svc.motion_commanded = True
        a.started = True
        a.updates += 1
        self._set_phase(a, svc.air_clock())
        return True
