"""Object-track executor (T4.2c): TRACK OBJECT from the accepted command to "Track ended at Lim EL".

Spec: BUILD_PLAN section 6 "T4.2 / 3. Executor". One `TrackExecutor` per gateway (held as
`ControlService.trackx`); at most one track at a time. It never writes to PWI4 itself except
through `ControlService.writer` (invariant I2) and the control service's own stop helpers.

State machine of a track (`TrackInfo.phase`):

  slewing   the rendezvous goto (an ordinary validated goto_altaz job with `target0`, D-SUN routed
            when the Sun is in the way) -> arrival (within ARRIVE_TOL_DEG, still). If the mount
            arrives later than t_r - LEAD_S the pass is re-planned ONCE (fresh check; one short
            extra goto when the new rendezvous is > REPLAN_GOTO_DEG away); late again -> stop.
  waiting   uploaded: tracking_off, wrap window, the monitor's route plan (look-ahead off), custom
            path new / add_point_list / apply(update_wrap=0). The mount holds still until t_r (P21).
  tracking  from t_r. "detour" while inside a planned Sun interval.
  end       at the last point + END_DELAY_S: stop + tracking_off, state override STOPPED
            "Track ended at Lim EL" (until the next command), audit `track_done`, snapshot
            `ended` for ENDED_TTL_S.

Long tracks (chunks of 30 min): when less than EXTEND_LEAD_S of path remain, the next chunk is
planned (`extend_track`) and appended (add_point_list + apply, no `new`, P22); the monitor's
route plan grows with it. A refused / gapped / out-of-window extension makes the current chunk the
last: the track ends at its end.

Timers use `ControlService.track_clock` (Unix seconds) and `track_sleep` (test seams). Cancellation:
`end()` (sync) closes the records, cancels the task and the rendezvous job; STOP / link loss /
safety stops arrive through `ControlService._cancel_all` -> `on_cancel`.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from typing import TYPE_CHECKING, Any

from . import dsun, state
from .commands import Command
from .control import (
    R_LINK_CANCELLED,
    R_STOP_CANCELLED,
    ROUTE_WRAP_FIT_DEG,
    ROUTE_WRAP_MARGIN_DEG,
    Pwi4Error,
    _Job,
    route_wrap_plan,
)
from .snapshot import TrackDetourInfo, TrackEndedInfo, TrackInfo
from .trackplan import (
    CODE_SETTING,
    R_SUN_APPROACHING,
    R_TRACK_ENDED,
    TrackPlan,
    TrackPoint,
    TrackRefusal,
    extend_track,
)

if TYPE_CHECKING:
    from .control import ControlService

R_GAP = "Track ended: the path has a gap"
R_WINDOW = "Track ended: azimuth window"
R_MISSED = "Missed the pass: could not reach the rendezvous in time"

ARRIVE_TOL_DEG = 0.3  # the mount is "at" the rendezvous within this (both axes) and still
ARRIVE_POLL_S = 0.2
REPLAN_GOTO_DEG = 0.5  # a re-planned rendezvous further than this from the pose needs a goto
END_DELAY_S = 1.0  # settle after the last path point before the stop
MONITOR_SETTLE_S = 3.0  # the monitor's route plan outlives the path by this
EXTEND_LEAD_S = 1200.0  # plan the next chunk when this much path remains
POLL_S = 1.0  # the follow loop wakes at least this often (health check)
ENDED_TTL_S = 30.0  # snapshot `track.ended` lasts this long
GAP_TOL_S = 1.5  # an extension starting later than chunk end + this is a gap


def _jd(t_unix: float) -> float:
    return t_unix / 86400.0 + 2440587.5


def _adopt_end(t: _Track, plan: TrackPlan) -> None:
    """A final plan that ends at a Sun interval sets the end reason / stop flag (+ its note)."""
    if plan.final and plan.end_reason != R_TRACK_ENDED:
        t.end_reason, t.sun_stop = plan.end_reason, plan.end_reason == R_SUN_APPROACHING
        t.notes.extend(n for n in plan.notes if n not in t.notes)


def _t_end(t: _Track) -> float | None:
    if t.sun_stop:
        return t.chunk_end
    return t.t_out if t.t_out is not None else (t.chunk_end if t.final else None)


class _Track:
    """One object track (mutable; owned by the executor)."""

    def __init__(self, conn_id: str, cmd: Command, tc: Any, job: _Job) -> None:
        self.conn_id = conn_id
        self.cmd = cmd
        self.norad: int = cmd.args["norad"]
        self.name: str = (tc.tle.name if tc.tle is not None and tc.tle.name else None) or (
            f"NORAD {self.norad}"
        )
        self.tle = tc.tle
        self.setup = tc.setup
        self.plan: TrackPlan = tc.plan
        pi = tc.pass_info or {}
        self.t_out: float | None = pi.get("t_out")
        self.t_peak: float | None = pi.get("t_peak")
        self.peak_alt: float | None = pi.get("peak_alt")
        self.job: _Job | None = job
        self.task: asyncio.Task | None = None
        self.phase = "slewing"
        self.t0 = time.monotonic()
        self.detours = list(tc.plan.detours)
        self.notes = list(tc.plan.notes)
        self.t_r = tc.plan.t_r
        # the uploaded path
        self.final = tc.plan.final
        self.end_reason: str | None = None
        self.sun_stop = False  # the path ends at a long Sun interval: t_end = the stop time
        self.chunk_end = tc.plan.t_end
        self.last_axis0 = tc.plan.points[-1].axis0
        self.wrap_min: float | None = None
        self.uploaded = False
        self.override_text: str | None = None
        self.replanned = False
        _adopt_end(self, tc.plan)


class TrackExecutor:
    def __init__(self, svc: ControlService) -> None:
        self.svc = svc
        self.t: _Track | None = None
        self.ended: TrackEndedInfo | None = None
        self.poll_s = POLL_S
        self._ended_override = False

    # ------------------------------------------------------------------ public state

    @property
    def active(self) -> bool:
        return self.t is not None

    @property
    def slewing(self) -> bool:
        return self.t is not None and self.t.phase == "slewing"

    def info(self) -> TrackInfo | None:
        """Snapshot `track` (poller provider): null when idle; only `ended` for ENDED_TTL_S after
        an end."""
        ended = self.ended
        if ended is not None and self.svc.track_clock() - ended.at > ENDED_TTL_S:
            self.ended = ended = None
        t = self.t
        if t is None:
            return TrackInfo(ended=ended) if ended is not None else None
        t_end = _t_end(t)
        return TrackInfo(
            norad=t.norad,
            name=t.name,
            phase=t.phase,
            t_r=t.t_r,
            t_end=t_end,
            t_peak=t.t_peak,
            peak_alt=t.peak_alt,
            detours=[TrackDetourInfo(t_a=d.t_a, t_b=d.t_b, side=d.side) for d in t.detours],
            notes=list(t.notes),
            ended=ended,
        )

    # ------------------------------------------------------------------ start / end

    def begin(self, conn_id: str, cmd: Command, tc: Any, job: _Job) -> None:
        """The command was accepted (verdict ok, rendezvous goto queued): run the track."""
        svc = self.svc
        t = _Track(conn_id, cmd, tc, job)
        self.t = t
        self.ended = None
        svc.audit.write(
            "track_start", client=conn_id, id=cmd.id, norad=t.norad, name=t.name,
            tle_epoch=getattr(t.tle, "epoch", None), t_r=round(t.t_r, 3),
            t_end=_t_end(t),
            detours=[[round(d.t_a, 1), round(d.t_b, 1), d.side] for d in t.detours],
            min_sun_sep=tc.plan.min_sun_sep_deg,
        )  # fmt: skip
        t.task = svc._spawn(self._main(t))
        svc.hub.republish_latest()

    def on_cancel(self, reason: str) -> None:
        """STOP / link loss / safety stop / stall / shutdown (`ControlService._cancel_all`)."""
        if self.t is None:
            return
        if reason == R_STOP_CANCELLED:
            text = "Stopped by STOP"
        elif reason == R_LINK_CANCELLED:
            text = "Control link lost"
        else:
            text = reason.removeprefix("Cancelled: ")
        self.end("track_stopped", text)

    def end(self, event: str, reason: str, *, quiet: bool = False) -> bool:
        """End the running track NOW (sync): records, task, rendezvous job. The caller stops the
        mount (STOP, the replacing command, the sequence). `event` track_stopped shows in the
        snapshot's `ended`; track_replaced does not. True when a track was running."""
        t = self.t
        if t is None:
            return False
        self._close(t, event, reason, ended=(event == "track_stopped" and not quiet), quiet=quiet)
        if t.task is not None and not t.task.done() and t.task is not asyncio.current_task():
            t.task.cancel()
        job = t.job
        if job is not None and not job.finished.is_set():
            job.cancelled_reason = reason
            if job.task is not None and not job.task.done():
                job.task.cancel()
            if not job.future.done():
                job.future.set_result((False, reason))
        return True

    def _close(
        self, t: _Track, event: str, reason: str, *, ended: bool, quiet: bool = False, **fields: Any
    ) -> bool:
        if self.t is not t:
            return False
        svc = self.svc
        self.t = None
        svc.motion_cleared()  # the monitor's route plan ends with the track
        ov = state.get_override()
        if ov is not None and ov[0] == "TRACKING":
            state.clear_override()
        if not quiet:
            svc.audit.write(
                event, client=t.conn_id, id=t.cmd.id, norad=t.norad, name=t.name, reason=reason,
                phase=t.phase, elapsed_s=round(time.monotonic() - t.t0, 1), **fields,
            )  # fmt: skip
        if ended:
            self.ended = TrackEndedInfo(norad=t.norad, reason=reason, at=svc.track_clock())
        svc.hub.republish_latest()
        return True

    def clear_ended_override(self) -> None:
        """The next command clears the "Track ended at Lim EL" state reason."""
        if not self._ended_override:
            return
        self._ended_override = False
        ov = state.get_override()
        if ov is not None and ov[0] == "STOPPED":
            state.clear_override()
            self.svc.hub.republish_latest()

    # ------------------------------------------------------------------ the task

    async def _sleep(self, seconds: float) -> None:
        await self.svc.track_sleep(max(0.0, seconds))

    async def _main(self, t: _Track) -> None:
        try:
            await self._flow(t)
        except asyncio.CancelledError:
            raise
        except Pwi4Error as exc:
            await self._abort(t, str(exc))
        except Exception as exc:  # a bug must still leave the mount safe and say so
            self.svc.audit.write("track_error", norad=t.norad, error=repr(exc))
            await self._abort(t, f"Track failed ({type(exc).__name__})")

    async def _abort(self, t: _Track, reason: str) -> None:
        """The track cannot go on: end it as stopped (with `reason`), stop the mount."""
        svc = self.svc
        if self.t is not t:
            return
        self._close(t, "track_stopped", reason, ended=True)
        with contextlib.suppress(Pwi4Error):
            await svc._stop_requests()
            await svc.writer.tracking_off()
        svc.motion_cleared()
        self._set_ended_override(reason)

    def _set_ended_override(self, reason: str) -> None:
        state.set_override("STOPPED", reason)
        self._ended_override = True
        self.svc.hub.republish_latest()

    async def _flow(self, t: _Track) -> None:
        svc = self.svc
        # 1. the rendezvous goto (queued by ControlService._track_command)
        await self._await_job(t)
        if self.t is not t:
            return
        if not await self._arrive(t):
            if not await self._replan(t):
                return
        # 2. the path
        await self._upload(t)
        if self.t is not t:
            return
        svc.hub.republish_latest()
        await self._follow(t)

    async def _await_job(self, t: _Track) -> None:
        job = t.job
        if job is None:
            return
        await job.finished.wait()
        if self.t is t and job.error:
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

    async def _arrive(self, t: _Track) -> bool:
        """Wait until the mount sits at the rendezvous. False when it is too late for t_r."""
        svc = self.svc
        rdv = t.plan.rendezvous
        while True:
            pose = self._pose()
            if pose is not None:
                if not pose[2].mount.is_connected:
                    raise Pwi4Error("Mount not connected")
                still = not svc.poller.tracker.moving_at(time.monotonic())
                if (
                    still
                    and abs(pose[0] - rdv.axis0) <= ARRIVE_TOL_DEG
                    and abs(pose[1] - rdv.alt) <= ARRIVE_TOL_DEG
                ):
                    return svc.track_clock() <= t.t_r - dsun.LEAD_S
            if svc.track_clock() > t.t_r - dsun.LEAD_S:
                return False
            await self._sleep(ARRIVE_POLL_S)

    async def _replan(self, t: _Track) -> bool:
        """The mount was late: plan the pass again from now (once). True = go on to the upload."""
        svc = self.svc
        if t.replanned:
            await self._abort(t, R_MISSED)
            return False
        t.replanned = True
        site = svc.poller.pwi4_site()
        pose = self._pose()
        if site is None or pose is None:
            await self._abort(t, "PWI4 not running / not reachable")
            return False
        tc = await asyncio.to_thread(svc.track_eval, t.norad, site, pose[2], True)
        if self.t is not t:
            return False
        if tc.reason or tc.plan is None or tc.setup is None:
            await self._abort(t, tc.reason or R_MISSED)
            return False
        old_t_r = t.t_r
        t.plan, t.setup = tc.plan, tc.setup
        t.t_r, t.final, t.chunk_end = tc.plan.t_r, tc.plan.final, tc.plan.t_end
        t.last_axis0 = tc.plan.points[-1].axis0
        t.detours = list(tc.plan.detours)
        t.notes = list(tc.plan.notes)
        t.end_reason, t.sun_stop = None, False
        _adopt_end(t, tc.plan)
        rdv = tc.plan.rendezvous
        dist = math.hypot(pose[0] - rdv.axis0, pose[1] - rdv.alt)
        svc.audit.write(
            "track_replan", norad=t.norad, old_t_r=round(old_t_r, 3), t_r=round(t.t_r, 3),
            move_deg=round(dist, 3),
        )  # fmt: skip
        svc.hub.republish_latest()
        if dist <= REPLAN_GOTO_DEG:
            return True
        goto = Command(
            f"{t.cmd.id}:rdv2", "goto_altaz",
            {"alt": rdv.alt, "az": rdv.az % 360.0, "target0": rdv.axis0},
        )  # fmt: skip
        v = svc._motion_verdict(goto, t.conn_id)
        if v.reason:
            await self._abort(t, v.reason)
            return False
        job = _Job(goto, t.conn_id)
        try:
            svc._queue.put_nowait(job)
        except asyncio.QueueFull:
            await self._abort(t, "Command queue full")
            return False
        t.job = job
        ok, reason = await job.future
        if not ok:
            if self.t is t:
                await self._abort(t, reason or "Command failed")
            return False
        await self._await_job(t)
        if self.t is not t:
            return False
        if not await self._arrive(t):
            await self._abort(t, R_MISSED)
            return False
        return True

    # ------------------------------------------------------------------ upload / extension

    @staticmethod
    def _wrap(t: _Track, plan: TrackPlan) -> tuple[float, list[TrackPoint], bool]:
        """(wrap_min, points to fly, truncated). A final chunk fits its own axis-0 range
        (route_wrap_plan); a non-final one anchors the window on the travel direction, clamped to
        the mechanical minimum, so later chunks have room (the window cannot move mid-path)."""
        pts = plan.points
        if plan.final:
            rps = [dsun.RoutePoint(p.t - plan.t_r, p.az, p.alt, p.axis0) for p in pts]
            w, kept, truncated = route_wrap_plan(rps)
            return w, pts[: len(kept)], truncated
        x0, x1 = pts[0].axis0, pts[-1].axis0
        w = x0 - 2.0 if x1 >= x0 else x0 + 2.0 - 359.0
        w = max(w, t.setup.mount.axis0_range[0])
        lo, hi = w + ROUTE_WRAP_MARGIN_DEG, w + ROUTE_WRAP_FIT_DEG + 1.0
        kept = []
        for p in pts:
            if not lo <= p.axis0 <= hi:
                break
            kept.append(p)
        return w, kept, len(kept) < len(pts)

    async def _upload(self, t: _Track) -> None:
        svc = self.svc
        pose = self._pose()
        if pose is None:
            raise Pwi4Error("PWI4 not running / not reachable")
        plan = t.plan
        wrap_min, kept, truncated = self._wrap(t, plan)
        if len(kept) < 2:
            raise Pwi4Error("Track does not fit the wrap window")
        if truncated:
            t.final = True
            t.end_reason = R_WINDOW
        now = svc.track_clock()
        last = kept[-1]
        total = max(0.0, last.t - now) + END_DELAY_S + MONITOR_SETTLE_S
        svc.motion_cleared()
        await svc.writer.tracking_off()
        # a STEP from sidereal TRACKING keeps its axis offset (T3.4, jog.py); it must not shift the
        # object track (Phase 4 end). Reset now that the mount no longer tracks.
        await svc.writer.mount_offset(axis0_reset=1, axis1_reset=1)
        cur = pose[2].mount.axis0_wrap_range_min_degs
        if cur is None or abs(cur - wrap_min) >= 0.05:
            await svc.writer.set_axis0_wrap_range_min(wrap_min)
        # the monitor: a route-type plan (actual limits + the 2 deg corridor measured on the sky,
        # Q19, no look-ahead)
        svc.route_begin(
            [(p.axis0, p.alt) for p in kept], (last.axis0, last.alt), total, None,
            (pose[0], pose[1]), sky=True,
        )  # fmt: skip
        await svc.writer.custom_path_new("altaz")
        await svc.writer.custom_path_add_points([(_jd(p.t), p.az, p.alt) for p in kept])
        await svc.writer.custom_path_apply(0)  # update_wrap=0 always (P8)
        svc.motion_commanded = True
        t.wrap_min, t.uploaded = wrap_min, True
        t.chunk_end, t.last_axis0 = last.t, last.axis0
        self._set_phase(t, svc.track_clock())

    async def _extend(self, t: _Track) -> None:
        """Append the next chunk (add + apply, no new). A refusal / gap / window exit makes the
        current chunk the last one."""
        svc = self.svc
        res = await asyncio.to_thread(extend_track, t.setup, t.chunk_end, t.last_axis0, t.t_out)
        if self.t is not t:
            return
        t.final = True  # unless the extension below succeeds and says otherwise
        if isinstance(res, TrackRefusal):
            t.end_reason = R_TRACK_ENDED if res.code == CODE_SETTING else res.reason
            t.sun_stop = t.sun_stop or res.reason == R_SUN_APPROACHING
            return
        pts = res.points
        w = t.wrap_min if t.wrap_min is not None else 0.0
        if res.t_r > t.chunk_end + GAP_TOL_S:
            t.end_reason = R_GAP
            return
        if (
            min(p.axis0 for p in pts) < w + ROUTE_WRAP_MARGIN_DEG
            or max(p.axis0 for p in pts) > w + ROUTE_WRAP_FIT_DEG + 1.0
        ):
            t.end_reason = R_WINDOW
            return
        await svc.writer.custom_path_add_points([(_jd(p.t), p.az, p.alt) for p in pts])
        await svc.writer.custom_path_apply(0)
        svc.route_extend(
            [(p.axis0, p.alt) for p in pts], (pts[-1].axis0, pts[-1].alt), res.t_end - t.chunk_end
        )
        svc.audit.write(
            "track_extend", norad=t.norad, points=len(pts), t_end=res.t_end, final=res.final,
            detours=len(res.detours),
        )  # fmt: skip
        t.final = res.final
        t.chunk_end, t.last_axis0 = res.t_end, pts[-1].axis0
        _adopt_end(t, res)
        t.detours.extend(res.detours)

    # ------------------------------------------------------------------ following

    def _set_phase(self, t: _Track, now: float) -> None:
        """Phase + the TRACKING state override from the clock."""
        if now < t.t_r:
            phase, text = "waiting", f"Waiting for {t.name}"
        elif any(d.t_a <= now < d.t_b for d in t.detours):
            phase, text = "detour", "Avoiding the Sun"
        else:
            phase, text = "tracking", f"Tracking {t.name}"
        if phase == t.phase and text == t.override_text:
            return
        t.phase, t.override_text = phase, text
        state.set_override("TRACKING", text)
        self.svc.hub.republish_latest()

    async def _follow(self, t: _Track) -> None:
        svc = self.svc
        while self.t is t:
            now = svc.track_clock()
            self._set_phase(t, now)
            if not t.final and t.chunk_end - now <= EXTEND_LEAD_S:
                await self._extend(t)
                continue
            if t.final and now >= t.chunk_end + END_DELAY_S:
                await self._finish(t)
                return
            pose = self._pose()
            if pose is not None and not pose[2].mount.is_connected:
                await self._abort(t, "Mount not connected")
                return
            events = [t.chunk_end + END_DELAY_S if t.final else t.chunk_end - EXTEND_LEAD_S]
            if now < t.t_r:
                events.append(t.t_r)
            for d in t.detours:
                events.extend(e for e in (d.t_a, d.t_b) if e > now)
            await self._sleep(min(max(min(events) - now, 0.0), self.poll_s))

    async def _finish(self, t: _Track) -> None:
        """The path is over: stop, tracking off, STOPPED with the reason, audit `track_done`."""
        svc = self.svc
        reason = t.end_reason or R_TRACK_ENDED
        err = None
        try:
            await svc._stop_requests()
            await svc.writer.tracking_off()
        except Pwi4Error as exc:
            err = str(exc)
        if self.t is not t:
            return
        if err is not None:
            reason = f"{reason}; STOP not delivered: {err}"
        self._close(t, "track_stopped" if err else "track_done", reason, ended=True,
                    t_end=round(t.chunk_end, 1))  # fmt: skip
        self._set_ended_override(reason)
        svc._spawn(svc._confirm_after_stop(t.conn_id, t.cmd.id))
