"""Command core (T3.1a): the ONLY module that writes to PWI4.

Invariant I2: every PWI4 write (`/mount/*`) is issued from `Pwi4Writer` below; nothing else in
the repository may call a PWI4 write endpoint. Never settings, never the site, never model
save/load or park-here.

Pieces:
- `Pwi4Writer`      time-limited async HTTP to PWI4 (stop, tracking, gotos, wrap minimum).
- `ControlService`  control lock, serialized command worker, STOP preemption, link-loss stop,
                    start-up stop, shutdown stop, audit. `check_command` is the seam T3.1b
                    (safety engine) fills in.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .audit import AuditLog
from .commands import MOTION_COMMANDS, Command, CommandError, parse_command
from .hub import Hub
from .poller import Poller
from .pwi4_io import PollResult, fetch_status, new_client, parse

WRITE_TIMEOUT_S = 1.0  # hard cap on one PWI4 write
ACK_TIMEOUT_S = 5.0  # a queued command that is not done by then is answered "timed out"
QUEUE_MAX = 8
MOVE_V_DEG_S = 0.05  # stopped = axis speed below this (P2: is_slewing lags, judge by position)
CONFIRM_POLL_S = 0.1
CONFIRM_RESEND_AFTER_S = 0.4  # still moving this long after a stop -> stop again
WRAP_MIN_EPS_DEG = 0.5

# G7 reasons
R_OTHER_HAS_CONTROL = "Another window has control"
R_NO_PWI4 = "PWI4 not running / not reachable"
R_NO_MOUNT = "Mount not connected"
R_STARTING = "Gateway starting up (checking mount state)"
R_LINK_STOP = "Stopping the mount after a lost control link"
R_SHUTTING = "Gateway shutting down"
R_BUSY = "Command queue full"
R_NOT_HOLDER = "This window does not have control"
R_STOP_CANCELLED = "Cancelled by STOP"
R_LINK_CANCELLED = "Cancelled: control link lost"


class Pwi4Error(Exception):
    pass


class Pwi4Writer:
    """The only PWI4 write path. Mirrors pwi4/pwi4_client.py endpoints and parameter names."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = base_url
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = new_client(self._base_url, self._transport)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def read_status(self) -> PollResult:
        """GET /status (a read; used to judge/confirm motion independently of the poller)."""
        return await fetch_status(self._http())

    async def _call(self, path: str, **params: Any) -> Any:
        """One time-limited GET. Returns the parsed status PWI4 answers with (or None)."""
        try:
            resp = await asyncio.wait_for(
                self._http().get(path, params=params or None), WRITE_TIMEOUT_S
            )
        except TimeoutError:
            raise Pwi4Error("PWI4 did not answer in time") from None
        except httpx.HTTPError as exc:
            raise Pwi4Error(f"PWI4 not reachable ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise Pwi4Error(f"PWI4 refused the request (HTTP {resp.status_code})")
        try:
            return parse(resp.content)
        except Exception:
            return None

    async def stop(self) -> Any:
        return await self._call("/mount/stop")

    async def tracking_on(self) -> Any:
        return await self._call("/mount/tracking_on")

    async def tracking_off(self) -> Any:
        return await self._call("/mount/tracking_off")

    async def goto_alt_az(self, alt_degs: float, az_degs: float) -> Any:
        return await self._call("/mount/goto_alt_az", alt_degs=alt_degs, az_degs=az_degs)

    async def goto_ra_dec_j2000(self, ra_hours: float, dec_degs: float) -> Any:
        return await self._call("/mount/goto_ra_dec_j2000", ra_hours=ra_hours, dec_degs=dec_degs)

    async def set_axis0_wrap_range_min(self, degs: float) -> Any:
        return await self._call("/mount/set_axis0_wrap_range_min", degs=degs)


def nearest_wrap_min(
    current_axis0: float, target_az: float, mech_min: float, mech_max: float
) -> float | None:
    """Axis-0 wrap minimum that makes PWI4 take the representation of `target_az` nearest to
    the current axis position (P8/P9). None when no representation fits the mechanical range.
    The window [wrap_min, wrap_min + 360) is centred on the chosen representation.
    """
    best: float | None = None
    k0 = math.floor((mech_min - target_az) / 360.0)
    for k in range(k0, k0 + 4):
        r = target_az + 360.0 * k
        if mech_min <= r <= mech_max and (
            best is None or abs(r - current_axis0) < abs(best - current_axis0)
        ):
            best = r
    return None if best is None else best - 180.0


def status_motion(status: Any) -> bool:
    """True when PWI4's flags say the mount is slewing or tracking (connected mounts only)."""
    m = status.mount
    return bool(m.is_connected and (m.is_slewing or m.is_tracking))


@dataclass
class CommandContext:
    """What the T3.1b safety hook may look at."""

    conn_id: str
    status: Any | None  # latest fresh PWI4Status (None when PWI4 is unreachable / stale)
    snapshot: Any | None  # latest MountSnapshot
    site_check: Any | None


CheckHook = Callable[[Command, CommandContext], str | None]


def check_command(cmd: Command, ctx: CommandContext) -> str | None:
    """Safety seam. T3.1b replaces this with the real safety engine (limits, Sun, site gate).
    Return a refusal reason, or None to allow. T3.1a: nothing to add.
    """
    return None


@dataclass
class _Job:
    cmd: Command
    conn_id: str
    future: asyncio.Future = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    cancelled_reason: str | None = None
    task: asyncio.Task | None = None


class ControlService:
    def __init__(
        self,
        writer: Pwi4Writer,
        poller: Poller,
        hub: Hub,
        audit: AuditLog,
        check: CheckHook = check_command,
    ) -> None:
        self.writer = writer
        self.poller = poller
        self.hub = hub
        self.audit = audit
        self.check: CheckHook = check
        # Azimuth of the last-checked goto_radec_j2000 target (T3.1b fills it in) for the wrap.
        self.resolved_az: Callable[[Command], float | None] = lambda cmd: None
        # Safety monitor hooks (T3.1b): a validated goto was sent / motion context reset.
        self.goto_started: Callable[[Command], None] = lambda cmd: None
        self.motion_cleared: Callable[[], None] = lambda: None
        self.holder: str | None = None
        self.ready = False
        self.shutting_down = False
        self.motion_commanded = False  # a motion command went out and no stop has confirmed
        self._link_busy = False
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._current: _Job | None = None
        self._worker: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self._startup_task: asyncio.Task | None = None
        hub.on_disconnect.append(self._on_disconnect)

    # ------------------------------------------------------------------ helpers

    def control_for(self, conn_id: str) -> dict[str, str]:
        if self.holder is None:
            return {"holder": "none"}
        return {"holder": "you" if self.holder == conn_id else "other"}

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def _ack(
        self, conn_id: str, cmd_id: str | None, cmd: str | None, ok: bool, reason: str | None
    ) -> dict[str, Any]:
        return {
            "type": "ack",
            "id": cmd_id,
            "cmd": cmd,
            "ok": ok,
            "reason": reason,
            "control": self.control_for(conn_id),
        }

    def _audit_cmd(self, conn_id: str, cmd: Command, ok: bool, reason: str | None, **extra) -> None:
        self.audit.write(
            "cmd",
            client=conn_id,
            id=cmd.id,
            cmd=cmd.name,
            args=cmd.args,
            result="ok" if ok else "refused",
            reason=reason,
            **extra,
        )

    def _set_holder(self, conn_id: str | None, how: str) -> None:
        prev = self.holder
        self.holder = conn_id
        if conn_id is not None:
            self.audit.write("lock_taken", client=conn_id, how=how, previous=prev)
        elif prev is not None:
            self.audit.write("lock_released", client=prev, how=how)
        self.hub.republish_latest()  # every window sees the new holder within one tick

    def fresh_status(self) -> Any | None:
        """Latest PWI4Status if the poller's data is fresh, else None."""
        p = self.poller
        good = p.last_good
        if good is None or p.last_result is None or not p.last_result.reachable:
            return None
        if time.monotonic() - p.last_good_t > p.stale_s:
            return None
        return good.status

    def _cached_motion(self) -> bool | None:
        """True / False when the cache is conclusive, None when unknown."""
        if self.motion_commanded:
            return True
        st = self.fresh_status()
        if st is None:
            return None
        if status_motion(st):
            return True
        return bool(self.poller.tracker.moving_at(time.monotonic()))

    # ------------------------------------------------------------------ inbound

    async def handle(self, conn_id: str, raw: str | bytes) -> dict[str, Any]:
        """Process one inbound frame from an authenticated connection; returns the ack.

        No await happens before the job is queued, so per-connection command order is kept.
        """
        try:
            cmd = parse_command(raw)
        except CommandError as exc:
            self.audit.write(
                "cmd", client=conn_id, id=exc.cmd_id, cmd=exc.cmd, result="refused",
                reason=exc.reason,
            )  # fmt: skip
            return self._ack(conn_id, exc.cmd_id, exc.cmd, False, exc.reason)

        name = cmd.name
        if name == "stop":
            return await self._stop_command(conn_id, cmd)
        if name == "take_control":
            if self.shutting_down:
                return self._refuse(conn_id, cmd, R_SHUTTING)
            if self.holder != conn_id:
                self._set_holder(conn_id, "take_control")
            self._audit_cmd(conn_id, cmd, True, None)
            return self._ack(conn_id, cmd.id, name, True, None)
        if name == "release_control":
            if self.holder != conn_id:
                return self._refuse(conn_id, cmd, R_NOT_HOLDER)
            self._set_holder(None, "release_control")
            self._audit_cmd(conn_id, cmd, True, None)
            return self._ack(conn_id, cmd.id, name, True, None)

        # ---- motion commands
        reason = self._motion_refusal(conn_id, cmd)
        if reason:
            return self._refuse(conn_id, cmd, reason)
        if self.holder is None:
            self._set_holder(conn_id, "first_command")
        job = _Job(cmd, conn_id)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            return self._refuse(conn_id, cmd, R_BUSY)
        try:
            ok, reason = await asyncio.wait_for(asyncio.shield(job.future), ACK_TIMEOUT_S)
        except TimeoutError:
            ok, reason = False, "Command timed out"
        self._audit_cmd(conn_id, cmd, ok, reason)
        return self._ack(conn_id, cmd.id, name, ok, reason)

    def _refuse(self, conn_id: str, cmd: Command, reason: str) -> dict[str, Any]:
        self._audit_cmd(conn_id, cmd, False, reason)
        return self._ack(conn_id, cmd.id, cmd.name, False, reason)

    def _motion_refusal(self, conn_id: str, cmd: Command) -> str | None:
        if self.shutting_down:
            return R_SHUTTING
        if not self.ready:
            return R_STARTING if self.poller.last_good is not None else R_NO_PWI4
        if self._link_busy:
            return R_LINK_STOP
        if self.holder is not None and self.holder != conn_id:
            return R_OTHER_HAS_CONTROL
        status = self.fresh_status()
        if status is None:
            return R_NO_PWI4
        if not status.mount.is_connected:
            return R_NO_MOUNT
        if cmd.name in MOTION_COMMANDS:
            for i, ax in enumerate((status.mount.axis0, status.mount.axis1)):
                if not ax.is_enabled:
                    return f"Axis {i} disabled"
        ctx = CommandContext(conn_id, status, self.poller.latest, self.poller.current_site_check())
        return self.check(cmd, ctx)

    # ------------------------------------------------------------------ worker

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._work())

    async def _work(self) -> None:
        while True:
            job = await self._queue.get()
            if job.cancelled_reason:
                continue
            self._current = job
            job.task = asyncio.create_task(self._execute(job))
            await asyncio.wait({job.task})  # a cancelled job task must not kill the worker
            self._current = None
            if not job.future.done():
                reason = job.cancelled_reason or "Command failed"
                job.future.set_result((False, reason))

    async def _execute(self, job: _Job) -> None:
        cmd = job.cmd
        try:
            if cmd.name == "goto_altaz":
                self.motion_cleared()
                # P15: PWI4's goto_alt_az does not end tracking; a local Az/El goto must end
                # STOPPED (logic sheet A5c-iv), so tracking goes off first.
                await self.writer.tracking_off()
                await self._prepare_wrap(cmd.args["az"])
                await self.writer.goto_alt_az(cmd.args["alt"], cmd.args["az"])
                self.motion_commanded = True
                self.goto_started(cmd)
            elif cmd.name == "goto_radec_j2000":
                az = self.resolved_az(cmd)
                if az is not None:
                    await self._prepare_wrap(az)
                self.motion_cleared()
                await self.writer.goto_ra_dec_j2000(cmd.args["ra_h"], cmd.args["dec_deg"])
                self.motion_commanded = True
                self.goto_started(cmd)
            elif cmd.name == "tracking":
                self.motion_cleared()
                if cmd.args["on"]:
                    await self.writer.tracking_on()
                    self.motion_commanded = True
                else:
                    await self.writer.tracking_off()
            if not job.future.done():
                job.future.set_result((True, None))
        except Pwi4Error as exc:
            if not job.future.done():
                job.future.set_result((False, str(exc)))
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.set_result((False, job.cancelled_reason or "Cancelled"))
            raise

    async def _prepare_wrap(self, target_az: float) -> None:
        """P8/P9: set the axis-0 wrap minimum so the nearest representation is taken."""
        st = self.fresh_status()
        if st is None:
            return
        ax = st.mount.axis0
        if None in (ax.position_degs, ax.min_mech_position_degs, ax.max_mech_position_degs):
            return
        want = nearest_wrap_min(
            ax.position_degs, target_az, ax.min_mech_position_degs, ax.max_mech_position_degs
        )
        cur = st.mount.axis0_wrap_range_min_degs
        if want is None or (cur is not None and abs(cur - want) < WRAP_MIN_EPS_DEG):
            return
        await self.writer.set_axis0_wrap_range_min(want)

    def _cancel_all(self, reason: str) -> int:
        """STOP preemption: drop everything queued, cancel the in-flight command."""
        n = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            job.cancelled_reason = reason
            if not job.future.done():
                job.future.set_result((False, reason))
            n += 1
        self.motion_cleared()
        cur = self._current
        if cur is not None:
            cur.cancelled_reason = reason
            if cur.task is not None and not cur.task.done():
                cur.task.cancel()
            n += 1
        return n

    # ------------------------------------------------------------------ stop

    async def _stop_requests(self) -> dict[str, Any]:
        """mount/stop (retried once). Measured on the sim (T3.1a): `mount/stop` also ends
        tracking, but the response's own is_tracking flag still shows the pre-stop value, so it
        cannot be used to decide; `_confirm_stopped` sends tracking_off if tracking persists.
        """
        t0 = time.perf_counter()
        for attempt in (1, 2):
            try:
                await self.writer.stop()
                break
            except Pwi4Error:
                if attempt == 2:
                    raise
        self.motion_commanded = False
        return {"stop_ms": round((time.perf_counter() - t0) * 1000.0, 1)}

    async def _confirm_stopped(self, max_s: float = 3.0) -> dict[str, Any]:
        """Read PWI4 until the axes are stationary and tracking is off; re-send stop /
        tracking-off when something is still going (a cancelled goto can land late).
        """
        t0 = time.perf_counter()
        last: tuple[float, float, float] | None = None
        still = 0
        last_stop = t0
        resent = 0
        while time.perf_counter() - t0 < max_s:
            res = await self.writer.read_status()
            now = time.perf_counter()
            if res.reachable:
                m = res.status.mount
                if not m.is_connected:
                    return self._confirm_result(True, t0, resent)
                p0, p1 = m.axis0.position_degs, m.axis1.position_degs
                vel = None
                if last is not None and p0 is not None and p1 is not None and now - last[0] > 0.02:
                    vel = max(abs(p0 - last[1]), abs(p1 - last[2])) / (now - last[0])
                if p0 is not None and p1 is not None:
                    last = (now, p0, p1)
                if m.is_tracking:
                    still = 0
                    with contextlib.suppress(Pwi4Error):
                        await self.writer.tracking_off()
                elif vel is not None and vel > MOVE_V_DEG_S:
                    still = 0
                    if now - last_stop > CONFIRM_RESEND_AFTER_S:
                        with contextlib.suppress(Pwi4Error):
                            await self.writer.stop()
                        last_stop = time.perf_counter()
                        resent += 1
                elif vel is not None:
                    still += 1
                    if still >= 2:
                        return self._confirm_result(True, t0, resent)
            await asyncio.sleep(CONFIRM_POLL_S)
        return self._confirm_result(False, t0, resent)

    @staticmethod
    def _confirm_result(ok: bool, t0: float, resent: int) -> dict[str, Any]:
        return {
            "confirmed": ok,
            "confirm_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "resent": resent,
        }

    async def _stop_command(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        """STOP: any authenticated connection, always, ahead of everything else."""
        t0 = time.perf_counter()
        cancelled = self._cancel_all(R_STOP_CANCELLED)
        try:
            info = await self._stop_requests()
        except Pwi4Error as exc:
            reason = f"STOP not delivered: {exc}"
            self.audit.write(
                "cmd", client=conn_id, id=cmd.id, cmd="stop", args={}, result="failed",
                reason=reason, cancelled=cancelled,
            )  # fmt: skip
            return self._ack(conn_id, cmd.id, "stop", False, reason)
        total_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        self.audit.write(
            "cmd", client=conn_id, id=cmd.id, cmd="stop", args={}, result="ok", reason=None,
            cancelled=cancelled, total_ms=total_ms, **info,
        )  # fmt: skip
        self._spawn(self._confirm_after_stop(conn_id, cmd.id))
        return self._ack(conn_id, cmd.id, "stop", True, None)

    async def safety_stop(self, reason: str, **details: Any) -> None:
        """Stop issued by the safety monitor (limit reached / predicted). Preempts like STOP."""
        t0 = time.perf_counter()
        cancelled = self._cancel_all(f"Cancelled: {reason}")
        try:
            info = await self._stop_requests()
            ok = True
        except Pwi4Error as exc:
            info, ok = {"error": str(exc)}, False
        self.audit.write(
            "limit_stop", reason=reason, delivered=ok, cancelled=cancelled,
            total_ms=round((time.perf_counter() - t0) * 1000.0, 1), **details, **info,
        )  # fmt: skip
        if ok:
            self._spawn(self._confirm_after_stop("safety", reason))

    async def _confirm_after_stop(self, conn_id: str, cmd_id: str | None) -> None:
        res = await self._confirm_stopped(2.0)
        self.audit.write("stop_confirm", client=conn_id, id=cmd_id, **res)

    # ------------------------------------------------------------------ link loss (G4, I3)

    def _on_disconnect(self, client: Any) -> None:
        if client.id == self.holder:
            self._spawn(self._holder_lost(client.id))

    async def _holder_lost(self, conn_id: str) -> None:
        t0 = time.perf_counter()
        self._link_busy = True
        try:
            cancelled = self._cancel_all(R_LINK_CANCELLED)
            active = self._cached_motion()
            if active is False:  # cache says idle: confirm with a fresh read before standing down
                res = await self.writer.read_status()
                active = status_motion(res.status) if res.reachable else None
            result: dict[str, Any] = {"motion_active": active, "cancelled": cancelled}
            if active is not False:  # active, or unknown -> stop (best effort when unknown)
                try:
                    result.update(await self._stop_requests())
                    result.update(await self._confirm_stopped(3.0))
                except Pwi4Error as exc:
                    result.update(confirmed=False, error=str(exc))
            result["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
            self.audit.write("link_loss_stop", client=conn_id, **result)
        finally:
            self._link_busy = False
            if self.holder == conn_id:
                self._set_holder(None, "link_loss")

    # ------------------------------------------------------------------ start-up / shutdown

    async def startup(self) -> None:
        """Before commands are accepted: stop any motion found at start (G4)."""
        res = None
        for _ in range(4):
            res = await self.writer.read_status()
            if res.reachable:
                break
            await asyncio.sleep(0.25)
        if res is None or not res.reachable:
            self.audit.write("startup", result="pwi4_unreachable", deferred=True)
            self._startup_task = asyncio.create_task(self._startup_retry())
            return
        await self._startup_with(res)

    async def _startup_retry(self) -> None:
        while not self.shutting_down:
            await asyncio.sleep(1.0)
            res = await self.writer.read_status()
            if res.reachable:
                await self._startup_with(res)
                return

    async def _startup_with(self, res: PollResult) -> None:
        t0 = time.perf_counter()
        m = res.status.mount
        active = status_motion(res.status)
        out: dict[str, Any] = {
            "mount_connected": bool(m.is_connected),
            "motion_active": active,
            "was_slewing": bool(m.is_slewing),
            "was_tracking": bool(m.is_tracking),
        }
        if active:
            try:
                out.update(await self._stop_requests())
                out.update(await self._confirm_stopped(3.0))
            except Pwi4Error as exc:
                out.update(confirmed=False, error=str(exc))
        out["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        self.audit.write("startup_stop" if active else "startup", **out)
        if not active or out.get("confirmed"):
            self.ready = True
        elif not self.shutting_down:
            # Could not confirm STOPPED: commands stay refused (STOP still works); keep trying.
            self._startup_task = asyncio.create_task(self._startup_retry())

    async def shutdown(self) -> None:
        """Graceful exit: no new commands, stop any motion, close the writer."""
        self.shutting_down = True
        self.ready = False
        self._cancel_all(R_SHUTTING)
        if self._startup_task is not None:
            self._startup_task.cancel()
        if self._bg:
            await asyncio.wait(set(self._bg), timeout=4.0)
        t0 = time.perf_counter()
        out: dict[str, Any] = {}
        try:
            res = await self.writer.read_status()
            if res.reachable:
                active = status_motion(res.status)
                out["motion_active"] = active
                if active:
                    out.update(await self._stop_requests())
                    out.update(await self._confirm_stopped(3.0))
            else:
                out["pwi4_reachable"] = False
        except Pwi4Error as exc:
            out.update(confirmed=False, error=str(exc))
        out["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        self.audit.write("shutdown_stop" if out.get("motion_active") else "shutdown", **out)
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        await self.writer.aclose()


def new_conn_id() -> str:
    return uuid.uuid4().hex[:8]
