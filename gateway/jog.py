"""Jog and step (T3.4c). Mechanics per BUILD_PLAN T3.4 "Q8 final, option A" and finding P20.

JOG (axis rate, deg/s of the mount axis, Q9). The client sends `jog {axis, dir, speed}` and re-sends
the same command every 100 ms while the key is held (keep-alive: no PWI4 call). A different
axis / dir / speed while jogging = switch. The gateway releases the jog when the keep-alives stop
for > KEEPALIVE_TIMEOUT_S, on `jog_stop`, on link loss, on STOP and on a safety / stall stop.

A JOG always ends STOPPED, whatever the start state (user decision 10-27). `origin` is kept in the
snapshot and the audit for information only.
- origin "stopped": the wrap window of axis 0 must contain the axis (P19: `tracking_on` would
  otherwise turn the mount up to 360 deg), then `tracking_on` + `axisN_set_rate_arcsec_per_sec`.
- origin "tracking": the rate offset only (no `tracking_on`, so no wrap check).
- Release / keep-alive timeout / disconnect (both origins): `axisN_stop_rate`, `mount/stop`,
  `tracking_off`, offsets reset (so a later TRACK starts clean) -> STOPPED.

STEP (`step {axis, dir, size_arcmin}`), one at a time:
- from TRACKING: `axisN_add_arcsec`; done when both axes' distance to target settled. It always
  ends TRACKING (offsetting a track): then `is_tracking` is verified and, if PWI4 stopped tracking,
  `tracking_on` is sent, but only with axis 0 inside its wrap window (P19); otherwise the mount
  ends STOPPED and the `step_end` audit carries note "Wrap out of range: tracking not resumed".
  The accumulated offset stays (no offset reset after a tracking step).
- from STOPPED: one validated `goto_alt_az` (pose + step; axis 0 -> azimuth, axis 1 -> altitude)
  through the normal goto path (tracking_off first, `_prepare_wrap`, safety plan) -> STOPPED.

AZIMUTH WRAP (T3.4e, option B). An axis-0 jog is a tracking rate offset, and PWI4 re-normalises
axis 0 into its 360 deg wrap window (`axis0_wrap_range_min` .. +360): at the window edge it would
swing the mount a silent 360 deg. So while an axis-0 jog runs the watcher keeps the window ahead
of the axis: within WRAP_SLIDE_MARGIN_DEG of the edge (in the jog direction) the window is
re-centred on the current position (`set_axis0_wrap_range_min(pos - 180)`, audit `wrap_slide`;
measured on the sim: no jump or turn while tracking with a rate offset). The real limit is the
mechanical range: the jog ends (normal end, STOPPED, audit reason `mech_limit`) at the margin
`jog_margin_deg(speed) + LIMIT_EXTRA_DEG` before it, and the snapshot's `jog_limit` asks the
user: `wrap_turn` (below) or stay (`wrap_turn_cancel`). A jog that starts at that margin in the
same direction is refused (`az_limit`) and sets `jog_limit` again; any axis-0 jog start clears it.

WRAP TURN (`wrap_turn {}`): a goto-like validated reposition to the SAME alt/az through the other
representation of axis 0 (target = position - dir * 360, inside the mechanical range, else
"No room for a 360 deg turn"). tracking_off, wrap window centred on the target, goto_alt_az. No key
is held, it ends STOPPED, STOP cancels it, the monitor judges it by its corridor plan. It goes
through `_motion_verdict` like a goto (Lim EL, Sun target, Sun PATH; D-SUN detours: T3.5).

All of it runs through `ControlService._motion_verdict` (the same verdict as the dry run) and the
single `Pwi4Writer`. A rate offset left behind by any other stop path is cleaned by `cleanup()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .commands import Command
from .control import (
    ACK_TIMEOUT_S,
    R_AZ_LIMIT,
    R_BUSY,
    R_JOG_SLEWING,
    R_JOGGING,
    R_LINK_CANCELLED,
    R_NO_PWI4,
    R_NO_ROOM,
    R_OTHER_HAS_CONTROL,
    R_SHUTTING,
    R_STEP,
    R_STOP_CANCELLED,
    R_TURN,
    R_WORKER_BUSY,
    Pwi4Error,
    _Job,
)
from .safety import SLEW_TAU_DEFAULT_S, jog_margin_deg, turn_duration_s
from .snapshot import JogInfo, JogLimitInfo, StepInfo, TurnInfo

if TYPE_CHECKING:
    from .control import ControlService

KEEPALIVE_TIMEOUT_S = 0.3
WATCH_PERIOD_S = 0.025
STEP_MIN_S = 0.3  # a step is never "done" sooner (the status may predate the offset)
STEP_TIMEOUT_S = 30.0
STEP_SETTLE_ARCSEC = 10.0  # tracking step: both axes within this of their target
STEP_ARRIVE_ARCSEC = 36.0  # stopped step (goto): ... and the mount no longer moving
WRAP_EPS_DEG = 0.01
LIMIT_EPS_DEG = 0.05  # a jog starting this close to a mechanical limit (into it) is refused
LIMIT_CODES = frozenset({"below_lim_el", "sun_target", "sun_path"})
# T3.4e
WRAP_SLIDE_MARGIN_DEG = 40.0  # re-centre the wrap window this far before the axis reaches its edge
WRAP_SLIDE_MIN_INTERVAL_S = 0.5  # the status may predate the previous slide
LIMIT_EXTRA_DEG = 2.0  # an axis-0 jog stops this far beyond its stopping distance before the limit
TURN_DEG = 360.0
TURN_ARRIVE_ARCSEC = 36.0
TURN_IDLE_S = 1.0  # the mount stands still short of the target this long: the turn ended


R_WRAP_NOT_RESUMED = "Wrap out of range: tracking not resumed"


def wrap_ok(m: Any) -> bool:
    """Axis 0 inside its wrap window (or unknown): `tracking_on` would not turn the mount (P19)."""
    wmin = m.axis0_wrap_range_min_degs
    p0 = m.axis0.position_degs
    if wmin is None or p0 is None:
        return True
    return wmin - WRAP_EPS_DEG <= p0 <= wmin + 360.0 + WRAP_EPS_DEG


def r_axis_limit(axis: int) -> str:
    return f"Axis {axis} at its mechanical limit"


def r_step_limit(axis: int) -> str:
    return f"Step goes past the axis {axis} limit"


def stop_reason(cancel_reason: str) -> str:
    """Audit `reason` of a jog ended by a preemption (`ControlService._cancel_all`)."""
    if cancel_reason == R_STOP_CANCELLED or cancel_reason == R_SHUTTING or cancel_reason == "stop":
        return "stop"
    if cancel_reason == R_LINK_CANCELLED:
        return "link_loss"
    return "limit"  # "Cancelled: <limit / Sun / corridor / stall>" from the safety paths


@dataclass
class JogState:
    axis: int
    dir: int
    speed: float
    origin: str  # "stopped" | "tracking"
    conn: str
    last_ka: float
    t0: float


@dataclass
class TurnPlan:
    """The 360 deg wrap turn from the current pose (axis-0 positions in degrees)."""

    from_deg: float
    to_deg: float
    dir: int  # direction of the jog that hit the limit (the turn goes the other way)
    alt: float
    az: float
    est_s: float


@dataclass
class TurnState:
    plan: TurnPlan
    conn: str
    t0: float


@dataclass
class LimitState:
    """The azimuth-limit prompt: where the jog stopped / was refused."""

    dir: int
    limit_deg: float
    threshold_deg: float  # axis-0 position of the stop margin (the prompt lapses when left)
    est_s: float


@dataclass
class StepState:
    axis: int
    dir: int
    size_arcmin: int
    origin: str
    t0: float


class JogController:
    def __init__(self, svc: ControlService) -> None:
        self.svc = svc
        self.jog: JogState | None = None
        self.step: StepState | None = None
        self._lock = asyncio.Lock()
        self._releasing = False
        self._watch_task: asyncio.Task | None = None
        self._start_task: asyncio.Task | None = None
        self.dirty = False  # rate offsets / tracking may be left behind by a preempted jog
        self.limit: LimitState | None = None  # T3.4e azimuth-limit prompt
        self.turn: TurnState | None = None  # T3.4e wrap turn in progress
        self._slide_t = -1e9

    # ------------------------------------------------------------------ snapshot / monitor

    def jog_info(self) -> JogInfo | None:
        j = self.jog
        if j is None:
            return None
        return JogInfo(axis=j.axis, dir=j.dir, speed=j.speed, origin=j.origin)

    def step_info(self) -> StepInfo | None:
        s = self.step
        return None if s is None else StepInfo(axis=s.axis, dir=s.dir, size_arcmin=s.size_arcmin)

    def limit_info(self) -> JogLimitInfo | None:
        """Snapshot `jog_limit`. Lapses once the axis has left the stop margin (another move)."""
        lim = self.limit
        if lim is None:
            return None
        st = self.svc.fresh_status()
        pos = None if st is None else st.mount.axis0.position_degs
        if pos is not None and lim.dir * (pos - lim.threshold_deg) < -1.0:
            self.limit = None
            return None
        return JogLimitInfo(
            axis=0, dir=lim.dir, limit_deg=lim.limit_deg, turn_deg=TURN_DEG, est_s=lim.est_s
        )

    def turn_info(self) -> TurnInfo | None:
        t = self.turn
        return None if t is None else TurnInfo(from_deg=t.plan.from_deg, to_deg=t.plan.to_deg)

    def probe(self) -> tuple[int, int, float] | None:
        """(axis, dir, speed) of the running jog, for the safety monitor / stall guard."""
        j = self.jog
        return None if j is None else (j.axis, j.dir, j.speed)

    # ------------------------------------------------------------------ verdict parts

    def _origin_now(self, status: Any) -> str:
        return "tracking" if status.mount.is_tracking else "stopped"

    def effective(self, cmd: Command) -> Command:
        """The command the safety hook judges: a step from STOPPED is a goto to pose + step."""
        st = self.svc.fresh_status()
        if cmd.name == "wrap_turn" and st is not None:
            plan = self.turn_plan(st.mount)
            if plan is None:
                return cmd
            args = {"alt": plan.alt, "az": plan.az, "target0": plan.to_deg, "dir": plan.dir}
            return Command(cmd.id, "wrap_turn", args)
        if cmd.name != "step" or st is None:
            return cmd
        m = st.mount
        if m.is_tracking or m.altitude_degs is None or m.azimuth_degs is None:
            return cmd
        d = cmd.args["dir"] * cmd.args["size_arcmin"] / 60.0
        alt, az = m.altitude_degs, m.azimuth_degs
        if cmd.args["axis"] == 0:
            az = (az + d) % 360.0
        else:
            alt += d
        return Command(cmd.id, "goto_altaz", {"alt": alt, "az": az})

    def turn_plan(self, m: Any) -> TurnPlan | None:
        """The wrap turn from the current pose, or None (no room / no position). Direction: that
        of the jog that hit the limit (`self.limit`); otherwise the way that fits, nearer the
        centre of the mechanical range."""
        ax = m.axis0
        pos, lo, hi = ax.position_degs, ax.min_mech_position_degs, ax.max_mech_position_degs
        alt, az = m.altitude_degs, m.azimuth_degs
        if None in (pos, lo, hi, alt, az):
            return None
        lim = self.limit
        dirs = (lim.dir,) if lim is not None else (1, -1)
        best: tuple[float, int] | None = None
        for d in dirs:
            to = pos - d * TURN_DEG
            if lo <= to <= hi and (best is None or abs(to) < abs(best[0])):
                best = (to, d)
        if best is None:
            return None
        to, d = best
        return TurnPlan(pos, to, d, alt, az, self._turn_est_s(m, pos, to))

    @staticmethod
    def _turn_est_s(m: Any, x0: float, x1: float) -> float:
        ax = m.axis0
        tau = getattr(m, "slew_time_constant", None) or SLEW_TAU_DEFAULT_S
        return turn_duration_s(
            x0,
            x1,
            ax.max_velocity_degs_per_sec or 20.0,
            ax.acceleration_degs_per_sec_sqr or 20.0,
            tau,
        )

    @staticmethod
    def az_margin_deg(m: Any, speed: float) -> float:
        """Axis-0 stop margin before the mechanical limit: the stopping distance as the safety
        monitor computes it plus LIMIT_EXTRA_DEG."""
        accel = getattr(m.axis0, "acceleration_degs_per_sec_sqr", None) or 20.0
        return jog_margin_deg(speed, accel) + LIMIT_EXTRA_DEG

    def at_az_limit(self, m: Any, d: int, speed: float) -> tuple[float, float] | None:
        """(mechanical limit, stop threshold) when axis 0 is at the stop margin in direction d."""
        ax = m.axis0
        pos = ax.position_degs
        lim = ax.max_mech_position_degs if d > 0 else ax.min_mech_position_degs
        if pos is None or lim is None:
            return None
        threshold = lim - d * self.az_margin_deg(m, speed)
        return (lim, threshold) if d * (pos - threshold) >= 0.0 else None

    def refusal(self, cmd: Command, status: Any) -> str | None:
        """Jog / step rules of the gateway verdict (after the axes / homed gates)."""
        name = cmd.name
        other = name in ("goto_altaz", "goto_radec_j2000", "tracking", "wrap_turn")
        if self._releasing and (other or name in ("jog", "step")):
            return R_WORKER_BUSY
        if name not in ("jog", "step"):
            if other:
                if self.turn is not None:
                    return R_TURN
                if self.step is not None:
                    return R_STEP
                if self.jog is not None:
                    return R_JOGGING
                if name == "wrap_turn":
                    snap = self.svc.poller.latest
                    if snap is not None and snap.state == "SLEWING":
                        return R_JOG_SLEWING
                    return None if self.turn_plan(status.mount) is not None else R_NO_ROOM
            return None
        if self.turn is not None:
            return R_TURN
        if self.step is not None:
            return R_STEP
        if name == "step" and self.jog is not None:
            return R_JOGGING
        snap = self.svc.poller.latest
        jogging = name == "jog" and self.jog is not None
        if not jogging and snap is not None and snap.state == "SLEWING":
            return R_JOG_SLEWING
        m = status.mount
        axis, d = cmd.args["axis"], cmd.args["dir"]
        ax = (m.axis0, m.axis1)[axis]
        pos, lo, hi = ax.position_degs, ax.min_mech_position_degs, ax.max_mech_position_degs
        if name == "jog":
            running = self.jog if jogging else None
            same_dir = running is not None and (running.axis, running.dir) == (axis, d)
            if axis == 0 and not same_dir and self.at_az_limit(m, d, cmd.args["speed"]):
                return R_AZ_LIMIT
            if pos is not None:
                if (d < 0 and lo is not None and pos <= lo + LIMIT_EPS_DEG) or (
                    d > 0 and hi is not None and pos >= hi - LIMIT_EPS_DEG
                ):
                    return r_axis_limit(axis)
        else:
            if pos is not None:
                new = pos + d * cmd.args["size_arcmin"] / 60.0
                if (lo is not None and new < lo) or (hi is not None and new > hi):
                    return r_step_limit(axis)
        return None

    # ------------------------------------------------------------------ commands

    async def handle(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        svc = self.svc
        if cmd.name == "jog_stop":
            if self.jog is None:
                return svc._ack(conn_id, cmd.id, cmd.name, True, None)
            if svc.holder is not None and svc.holder != conn_id:
                return svc._refuse(conn_id, cmd, R_OTHER_HAS_CONTROL)
            await self.release("release")
            return svc._ack(conn_id, cmd.id, cmd.name, True, None)
        if cmd.name == "jog":
            return await self._jog(conn_id, cmd)
        if cmd.name == "wrap_turn":
            return await self._turn(conn_id, cmd)
        if cmd.name == "wrap_turn_cancel":
            return self._turn_cancel(conn_id, cmd)
        return await self._step(conn_id, cmd)

    # -- jog

    async def _jog(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        svc = self.svc
        j = self.jog
        a = cmd.args
        same = (
            j is not None
            and j.conn == conn_id
            and (j.axis, j.dir, j.speed) == (a["axis"], a["dir"], a["speed"])
        )
        v = svc._motion_verdict(cmd, conn_id)
        if v.code == "az_limit" and v.reason:
            st = svc.fresh_status()
            if st is not None:
                if self._set_limit(st.mount, a["dir"], a["speed"], "refused"):
                    return svc._refuse(conn_id, cmd, v.reason, v.code)
                # a held key re-sends: already prompting, no audit per 100 ms
                return svc._ack(conn_id, cmd.id, cmd.name, False, v.reason, v.code)
        if v.reason:
            if same:  # a refused keep-alive: not audited per 100 ms; a limit ends the jog
                if v.code in LIMIT_CODES:
                    await self.release("limit")
                return svc._ack(conn_id, cmd.id, cmd.name, False, v.reason, v.code)
            return svc._refuse(conn_id, cmd, v.reason, v.code)
        if svc.holder is None:
            svc._set_holder(conn_id, "first_command")
        if same and j is not None:
            j.last_ka = time.monotonic()  # keep-alive: no PWI4 call, no audit
            return svc._ack(conn_id, cmd.id, cmd.name, True, None)
        if svc._current is not None or not svc._queue.empty():
            return svc._refuse(conn_id, cmd, R_WORKER_BUSY)
        task = asyncio.create_task(self._start(conn_id, cmd))
        self._start_task = task
        try:
            await asyncio.wait({task})
        finally:
            if self._start_task is task:
                self._start_task = None
        ok, reason = (False, R_STOP_CANCELLED) if task.cancelled() else task.result()
        svc._audit_cmd(conn_id, cmd, ok, reason)
        return svc._ack(conn_id, cmd.id, cmd.name, ok, reason)

    async def _start(self, conn_id: str, cmd: Command) -> tuple[bool, str | None]:
        """Start or switch (under the lock). Mutates `self.jog` first, so keep-alives that arrive
        while the PWI4 calls are in flight are accepted."""
        svc, w = self.svc, self.svc.writer
        a = cmd.args
        async with self._lock:
            prev = self.jog
            st = svc.fresh_status()
            if st is None:
                return False, "PWI4 not running / not reachable"
            origin = prev.origin if prev is not None else self._origin_now(st)
            now = time.monotonic()
            self.jog = JogState(a["axis"], a["dir"], a["speed"], origin, conn_id, now, now)
            rate = a["dir"] * a["speed"] * 3600.0
            try:
                self.dirty = True
                if prev is None:
                    svc.motion_cleared()
                    if origin == "stopped":
                        # Option B (T3.4e): an axis outside PWI4's wrap window would make
                        # tracking_on turn the mount up to 360 deg (P19). Re-centre the window on
                        # the axis instead of refusing: on a stopped mount nothing moves.
                        await self._recentre_wrap(st.mount, "jog_start")
                        await w.tracking_on()
                        svc.motion_commanded = True
                    await w.mount_offset(**{f"axis{a['axis']}_set_rate_arcsec_per_sec": rate})
                else:
                    params: dict[str, Any] = {f"axis{a['axis']}_set_rate_arcsec_per_sec": rate}
                    if prev.axis != a["axis"]:
                        params[f"axis{prev.axis}_stop_rate"] = 1
                    await w.mount_offset(**params)
            except Pwi4Error as exc:
                if self.jog is not None:  # not already cleared by STOP
                    self.jog = None
                    await self._end_hard(origin)
                return False, str(exc)
            if self.jog is None:  # STOP / safety arrived meanwhile
                return False, "Cancelled by STOP"
            if a["axis"] == 0 and self.limit is not None:
                self.limit = None  # a new axis-0 jog: the prompt lapses (it returns at the margin)
            svc.audit.write(
                "jog_start", client=conn_id, id=cmd.id, axis=a["axis"], dir=a["dir"],
                speed=a["speed"], origin=origin, switch=prev is not None,
            )  # fmt: skip
            if self._watch_task is None or self._watch_task.done():
                self._watch_task = svc._spawn(self._watch())
            svc.hub.republish_latest()
            return True, None

    async def _end_hard(self, origin: str) -> None:
        """A failed start: undo what may have gone out (best effort)."""
        svc, w = self.svc, self.svc.writer
        with contextlib.suppress(Pwi4Error):
            await w.mount_offset(axis0_stop_rate=1, axis1_stop_rate=1)
        with contextlib.suppress(Pwi4Error):
            await svc._stop_requests()
        with contextlib.suppress(Pwi4Error):
            await w.tracking_off()
        with contextlib.suppress(Pwi4Error):
            await w.mount_offset(axis0_reset=1, axis1_reset=1)
        self.dirty = False

    async def _watch(self) -> None:
        """Dead-man: the keep-alives must keep coming (I3 for jog)."""
        while self.jog is not None:
            await asyncio.sleep(WATCH_PERIOD_S)
            j = self.jog
            if j is None:
                break
            if time.monotonic() - j.last_ka > KEEPALIVE_TIMEOUT_S:
                await self.release("keepalive_timeout")
            elif j.axis == 0:
                await self._guard_axis0(j)

    async def _guard_axis0(self, j: JogState) -> None:
        """T3.4e: end the jog at the mechanical-limit margin, else keep the wrap window ahead."""
        svc = self.svc
        st = svc.fresh_status()
        if st is None:
            return
        m = st.mount
        pos, wmin = m.axis0.position_degs, m.axis0_wrap_range_min_degs
        if pos is None:
            return
        if self.at_az_limit(m, j.dir, j.speed) is not None:
            if await self.release("mech_limit", only=j):
                self._set_limit(m, j.dir, j.speed, "jog_end")
            return
        now = time.monotonic()
        if wmin is None or now - self._slide_t < WRAP_SLIDE_MIN_INTERVAL_S:
            return
        edge_dist = (wmin + 360.0 - pos) if j.dir > 0 else (pos - wmin)
        if edge_dist >= WRAP_SLIDE_MARGIN_DEG:
            return
        self._slide_t = now
        new_min = pos - 180.0
        try:
            async with self._lock:
                if self.jog is not j:
                    return
                await svc.writer.set_axis0_wrap_range_min(new_min)
        except Pwi4Error as exc:  # without the slide PWI4 would turn 360 deg at the edge
            svc.audit.write("wrap_slide_failed", error=str(exc), pos=round(pos, 3))
            await self.release("wrap_slide_failed", only=j)
            return
        svc.audit.write(
            "wrap_slide", axis=0, dir=j.dir, pos=round(pos, 3), old_min=round(wmin, 3),
            new_min=round(new_min, 3),
        )  # fmt: skip

    def _set_limit(self, m: Any, d: int, speed: float, how: str) -> bool:
        """Set the azimuth-limit prompt. True when it was newly set (audited once)."""
        ax = m.axis0
        limit_deg = ax.max_mech_position_degs if d > 0 else ax.min_mech_position_degs
        if limit_deg is None:
            return False
        threshold = limit_deg - d * self.az_margin_deg(m, speed)
        pos = ax.position_degs
        est = self._turn_est_s(m, pos if pos is not None else threshold, threshold - d * TURN_DEG)
        prev = self.limit
        self.limit = LimitState(d, limit_deg, threshold, est)
        new = prev is None or prev.dir != d
        if new:
            self.svc.audit.write(
                "jog_limit", axis=0, dir=d, limit_deg=limit_deg, pos=pos, how=how, est_s=est,
                margin_deg=round(abs(limit_deg - threshold), 2),
            )  # fmt: skip
        self.svc.hub.republish_latest()
        return new

    async def release(self, reason: str, only: JogState | None = None) -> bool:
        """Normal end of a jog (release / keep-alive timeout / limit seen by the gateway). With
        `only`, just that jog (not a newer one). True when a jog was ended."""
        svc, w = self.svc, self.svc.writer
        async with self._lock:
            j = self.jog
            if j is None or (only is not None and j is not only):
                return False
            self.jog = None
            self._releasing = True
            t0 = time.monotonic()
            errors: list[str] = []
            try:
                try:
                    await w.mount_offset(**{f"axis{j.axis}_stop_rate": 1})
                except Pwi4Error as exc:
                    errors.append(str(exc))
                try:
                    await svc._stop_requests()
                except Pwi4Error as exc:
                    errors.append(str(exc))
                try:
                    await w.tracking_off()
                    await w.mount_offset(axis0_reset=1, axis1_reset=1)
                except Pwi4Error as exc:
                    errors.append(str(exc))
                svc._spawn(svc._confirm_after_stop(j.conn, "jog"))
                self.dirty = bool(errors)
            finally:
                self._releasing = False
            extra: dict[str, Any] = {"error": errors[0]} if errors else {}
            svc.audit.write(
                "jog_stop", client=j.conn, reason=reason, origin=j.origin, axis=j.axis,
                dir=j.dir, speed=j.speed, held_s=round(t0 - j.t0, 2),
                release_ms=round((time.monotonic() - t0) * 1000.0, 1), **extra,
            )  # fmt: skip
            svc.hub.republish_latest()
            return True

    # -- wrap turn (T3.4e)

    async def _turn(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        svc = self.svc
        v = svc._motion_verdict(cmd, conn_id)
        if v.reason:
            return svc._refuse(conn_id, cmd, v.reason, v.code)
        st = svc.fresh_status()
        if st is None:
            return svc._refuse(conn_id, cmd, R_NO_PWI4)
        plan = self.turn_plan(st.mount)  # no await since the verdict: the plan the hook judged
        if plan is None:
            return svc._refuse(conn_id, cmd, R_NO_ROOM, "no_room")
        eff = self.effective(cmd)
        if svc.holder is None:
            svc._set_holder(conn_id, "first_command")
        if svc._current is not None or not svc._queue.empty():
            return svc._refuse(conn_id, cmd, R_WORKER_BUSY)
        job = _Job(eff, conn_id)
        try:
            svc._queue.put_nowait(job)
        except asyncio.QueueFull:
            return svc._refuse(conn_id, cmd, R_BUSY)
        try:
            ok, reason = await asyncio.wait_for(asyncio.shield(job.future), ACK_TIMEOUT_S)
        except TimeoutError:
            ok, reason = False, "Command timed out"
        if ok:
            turn = TurnState(plan, conn_id, time.monotonic())
            self.turn = turn
            self.limit = None
            svc.audit.write(
                "wrap_turn", client=conn_id, id=cmd.id, from_deg=round(plan.from_deg, 3),
                to_deg=round(plan.to_deg, 3), dir=plan.dir, est_s=plan.est_s,
                alt=round(plan.alt, 4), az=round(plan.az, 4),
            )  # fmt: skip
            svc._spawn(self._watch_turn(turn))
            svc.hub.republish_latest()
        svc._audit_cmd(conn_id, cmd, ok, reason)
        return svc._ack(conn_id, cmd.id, cmd.name, ok, reason)

    def _turn_cancel(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        """Stay: the mount is already stopped at the limit; just drop the prompt."""
        svc = self.svc
        if svc.holder is not None and svc.holder != conn_id:
            return svc._refuse(conn_id, cmd, R_OTHER_HAS_CONTROL)
        had = self.limit is not None
        self.limit = None
        svc.audit.write("wrap_turn_cancel", client=conn_id, id=cmd.id, had_limit=had)
        svc.hub.republish_latest()
        return svc._ack(conn_id, cmd.id, cmd.name, True, None)

    async def _watch_turn(self, turn: TurnState) -> None:
        svc = self.svc
        why = "done"
        idle_t: float | None = None
        limit_s = turn.plan.est_s * 1.5 + 10.0
        while self.turn is turn:
            await asyncio.sleep(WATCH_PERIOD_S)
            if svc.route is not None:  # T3.5c: a D-SUN route flies the sweep: not timed, not idle
                # (t0 follows the clock: the final goto of a truncated sweep is sent right as the
                # route ends, and a status that predates it must not read as "arrived")
                turn.t0 = time.monotonic()
                idle_t = None
                continue
            now = time.monotonic()
            el = now - turn.t0
            if el > limit_s:
                why = "timeout"
                break
            st = svc.fresh_status()
            if st is None or el < STEP_MIN_S:
                continue
            m = st.mount
            d0 = abs(m.axis0.dist_to_target_arcsec or 0.0)
            d1 = abs(m.axis1.dist_to_target_arcsec or 0.0)
            still = not m.is_slewing and not m.is_tracking and not svc.poller.tracker.moving_at(now)
            if max(d0, d1) <= TURN_ARRIVE_ARCSEC and still:
                break
            if still and el > 1.0:  # stopped short (someone else's stop)
                idle_t = idle_t or now
                if now - idle_t > TURN_IDLE_S:
                    why = "stopped"
                    break
            else:
                idle_t = None
        if self.turn is turn:
            self.turn = None
            st = svc.fresh_status()
            pos = None if st is None else st.mount.axis0.position_degs
            svc.audit.write(
                "wrap_turn_end", how=why, from_deg=round(turn.plan.from_deg, 3),
                to_deg=round(turn.plan.to_deg, 3), final_deg=pos if pos is None else round(pos, 3),
                elapsed_s=round(time.monotonic() - turn.t0, 2), est_s=turn.plan.est_s,
            )  # fmt: skip
            svc.hub.republish_latest()

    # -- step

    async def _step(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        svc = self.svc
        v = svc._motion_verdict(cmd, conn_id)
        if v.reason:
            return svc._refuse(conn_id, cmd, v.reason, v.code)
        st = svc.fresh_status()
        if st is None:
            return svc._refuse(conn_id, cmd, "PWI4 not running / not reachable")
        eff = self.effective(cmd)  # no await since the verdict: same status, same plan id
        origin = "stopped" if eff.name == "goto_altaz" else "tracking"
        a = cmd.args
        if svc.holder is None:
            svc._set_holder(conn_id, "first_command")
        if svc._current is not None or not svc._queue.empty():
            return svc._refuse(conn_id, cmd, R_WORKER_BUSY)
        step = StepState(a["axis"], a["dir"], a["size_arcmin"], origin, time.monotonic())
        self.step = step
        svc.audit.write(
            "step", client=conn_id, id=cmd.id, axis=a["axis"], dir=a["dir"],
            size_arcmin=a["size_arcmin"], origin=origin,
        )  # fmt: skip
        ok, reason = True, None
        try:
            if origin == "tracking":
                async with self._lock:
                    await svc.writer.mount_offset(
                        **{f"axis{a['axis']}_add_arcsec": a["dir"] * a["size_arcmin"] * 60.0}
                    )
                self.dirty = True
            else:
                job = _Job(eff, conn_id)
                try:
                    svc._queue.put_nowait(job)
                except asyncio.QueueFull:
                    raise Pwi4Error(R_BUSY) from None
                try:
                    ok, reason = await asyncio.wait_for(asyncio.shield(job.future), ACK_TIMEOUT_S)
                except TimeoutError:
                    ok, reason = False, "Command timed out"
        except Pwi4Error as exc:
            ok, reason = False, str(exc)
        if not ok:
            if self.step is step:
                self.step = None
        else:
            svc._spawn(self._watch_step(step))
            svc.hub.republish_latest()
        svc._audit_cmd(conn_id, cmd, ok, reason)
        return svc._ack(conn_id, cmd.id, cmd.name, ok, reason)

    async def _watch_step(self, step: StepState) -> None:
        svc = self.svc
        why = "done"
        while self.step is step:
            await asyncio.sleep(WATCH_PERIOD_S)
            now = time.monotonic()
            el = now - step.t0
            if el > STEP_TIMEOUT_S:
                why = "timeout"
                break
            st = svc.fresh_status()
            if st is None or el < STEP_MIN_S:
                continue
            m = st.mount
            d0 = abs(m.axis0.dist_to_target_arcsec or 0.0)
            d1 = abs(m.axis1.dist_to_target_arcsec or 0.0)
            if step.origin == "tracking":
                if max(d0, d1) <= STEP_SETTLE_ARCSEC:
                    break
            elif (
                max(d0, d1) <= STEP_ARRIVE_ARCSEC
                and not m.is_slewing
                and not m.is_tracking
                and not svc.poller.tracker.moving_at(now)
            ):
                break  # PWI4 reports slewing / tracking until the goto has really arrived
        extra: dict[str, Any] = {}
        if self.step is step and step.origin == "tracking":
            extra = await self._resume_tracking(step)
        if self.step is step:
            self.step = None
            svc.audit.write(
                "step_end", axis=step.axis, dir=step.dir, size_arcmin=step.size_arcmin,
                origin=step.origin, how=why, elapsed_s=round(time.monotonic() - step.t0, 2),
                **extra,
            )  # fmt: skip
            svc.hub.republish_latest()

    async def _recentre_wrap(self, m: Any, why: str) -> None:
        """Before `tracking_on`: put axis 0 inside PWI4's wrap window (centred on it) so PWI4 does
        not turn the mount to normalise it (P19). No-op when it is already inside."""
        if wrap_ok(m):
            return
        p0 = m.axis0.position_degs
        new_min = p0 - 180.0
        await self.svc.writer.set_axis0_wrap_range_min(new_min)
        self.svc.audit.write(
            "wrap_recentre", why=why, pos=round(p0, 3),
            old_min=m.axis0_wrap_range_min_degs, new_min=round(new_min, 3),
        )  # fmt: skip

    async def _resume_tracking(self, step: StepState) -> dict[str, Any]:
        """A tracking step ends TRACKING: if PWI4 stopped tracking meanwhile, send `tracking_on`
        (only with axis 0 inside its wrap window, P19; else the mount stays STOPPED)."""
        svc = self.svc
        st = svc.fresh_status()
        if st is None or st.mount.is_tracking:
            return {"tracking_resumed": False}
        try:
            async with self._lock:
                if self.step is not step:  # STOP / safety arrived meanwhile
                    return {"tracking_resumed": False}
                await self._recentre_wrap(st.mount, "step_resume")
                await svc.writer.tracking_on()
                svc.motion_commanded = True
        except Pwi4Error as exc:
            return {"tracking_resumed": False, "note": str(exc)}
        return {"tracking_resumed": True}

    # ------------------------------------------------------------------ preemption

    def on_cancel(self, reason: str) -> None:
        """STOP / link loss / safety / stall / shutdown (`ControlService._cancel_all`): the jog and
        step end now; `cleanup()` (after the stop is delivered) removes the rate offsets."""
        j, s, t = self.jog, self.step, self.turn
        if t is not None:
            self.turn = None
            self.svc.audit.write(
                "wrap_turn_end", how=stop_reason(reason), from_deg=round(t.plan.from_deg, 3),
                to_deg=round(t.plan.to_deg, 3), elapsed_s=round(time.monotonic() - t.t0, 2),
                est_s=t.plan.est_s,
            )  # fmt: skip
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            self.dirty = True
        if j is not None:
            self.jog = None
            self.dirty = True
            self.svc.audit.write(
                "jog_stop", client=j.conn, reason=stop_reason(reason), origin=j.origin,
                axis=j.axis, dir=j.dir, speed=j.speed, held_s=round(time.monotonic() - j.t0, 2),
            )  # fmt: skip
        if s is not None:
            self.step = None
            self.dirty = True
            self.svc.audit.write(
                "step_end", axis=s.axis, dir=s.dir, size_arcmin=s.size_arcmin, origin=s.origin,
                how=stop_reason(reason), elapsed_s=round(time.monotonic() - s.t0, 2),
            )  # fmt: skip
        if self._releasing:
            self.dirty = True

    async def cleanup(self) -> None:
        """After a stop was delivered: no rate offset may survive (a later TRACK would drift) and
        the offsets are reset (only now that the mount no longer tracks: a reset on a tracking
        mount snaps it back)."""
        if not self.dirty:
            return
        w = self.svc.writer
        self.dirty = False
        try:
            await w.mount_offset(axis0_stop_rate=1, axis1_stop_rate=1)
            await w.tracking_off()
            await w.mount_offset(axis0_reset=1, axis1_reset=1)
        except Pwi4Error as exc:
            self.dirty = True
            self.svc.audit.write("jog_cleanup_failed", error=str(exc))
