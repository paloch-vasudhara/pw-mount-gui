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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from . import state
from .audit import AuditLog
from .commands import MOTION_COMMANDS, SEQUENCE_COMMANDS, Command, CommandError, parse_command
from .homed import R_NOT_HOMED, R_STALLED, HomedGuard
from .hub import Hub
from .poller import Poller
from .pwi4_io import CONNECT_TIMEOUT_S, PollResult, fetch_status, new_client, parse
from .snapshot import RouteInfo, jd_from_utc

if TYPE_CHECKING:
    from . import dsun

WRITE_TIMEOUT_S = 1.0  # hard cap on one PWI4 write
ACK_TIMEOUT_S = 5.0  # a queued command that is not done by then is answered "timed out"
QUEUE_MAX = 8
MOVE_V_DEG_S = 0.05  # stopped = axis speed below this (P2: is_slewing lags, judge by position)
CONFIRM_POLL_S = 0.1
CONFIRM_RESEND_AFTER_S = 0.4  # still moving this long after a stop -> stop again
WRAP_MIN_EPS_DEG = 0.5
SLOW_WRITE_TIMEOUT_S = 8.0  # mount/connect, disconnect, enable, disable, find_home (T3.2)
PATH_WRITE_TIMEOUT_S = 3.0  # custom path upload / apply (T3.5c)
# D-SUN route execution (T3.5c, P21)
ROUTE_SETTLE_S = 1.0  # wait after the last route point before the final command (P21: ~0.5 s)
ROUTE_REASON = "Avoiding the Sun"  # Q12: state_reason while a route flies
ROUTE_WRAP_MARGIN_DEG = 1.0  # the route's axis-0 range sits this far inside the wrap window
ROUTE_WRAP_FIT_DEG = 357.0  # a route spanning more cannot fit a 360 deg window with the margin
ROUTE_POSE_TOL_DEG = 2.0  # the mount must still be this close to the validated start pose

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
R_WORKER_BUSY = "Another command is still running"
R_SLEWING = "Mount is slewing; press STOP first"
R_NOT_HOMED_STATE = "Mount is not in the not-homed state"
# T3.4c jog / step
R_JOG_SLEWING = "Slewing; press STOP first"
R_STEP = "Step in progress"
R_JOGGING = "Jog in progress; release first"
R_WRAP = "Wrap out of range: re-home or Go To first"
# T3.4e azimuth wrap handling
R_AZ_LIMIT = "Azimuth at its mechanical limit: turn 360° or jog the other way"
R_NO_ROOM = "No room for a 360° turn"
R_TURN = "Turn in progress; press STOP first"
R_ROUTING = "Avoiding the Sun; press STOP first"
# Commands refused while the mount is not homed (T3.3). HOME, STOP, DISCONNECT, tracking off and
# the lock commands are never gated; a future jog joins this set.
ROUTABLE = frozenset({"goto_altaz", "goto_radec_j2000", "wrap_turn"})  # T3.5c: D-SUN routed
HOMED_GATED = frozenset(
    {"goto_altaz", "goto_radec_j2000", "jog", "step", "wrap_turn", "track_object"}
)
R_TRACK_NA = "Object tracking arrives in T4.2"
CODE_HOME_BLOCKED = "home_blocked_confirm"  # disconnect: home is blocked, ask the user once
_REASON_CODES: dict[str, str] = {
    R_SHUTTING: "shutting_down",
    R_STARTING: "starting",
    R_NO_PWI4: "no_pwi4",
    R_LINK_STOP: "link_stop",
    R_NO_MOUNT: "no_mount",
    R_SLEWING: "slewing",
    R_NOT_HOMED: "not_homed",
    R_STALLED: "stalled",
    R_OTHER_HAS_CONTROL: "other_has_control",
    R_JOG_SLEWING: "slewing",
    R_STEP: "step_in_progress",
    R_JOGGING: "jog_in_progress",
    R_WRAP: "wrap_out_of_range",
    R_AZ_LIMIT: "az_limit",
    R_NO_ROOM: "no_room",
    R_TURN: "turn_in_progress",
    R_ROUTING: "slewing",
}


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

    async def _call(self, path: str, _timeout: float = WRITE_TIMEOUT_S, **params: Any) -> Any:
        """One time-limited GET. Returns the parsed status PWI4 answers with (or None)."""
        try:
            resp = await asyncio.wait_for(
                self._http().get(
                    path,
                    params=params or None,
                    timeout=httpx.Timeout(_timeout, connect=CONNECT_TIMEOUT_S),
                ),
                _timeout,
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

    async def mount_offset(self, **params: Any) -> Any:
        """/mount/offset (T3.4c jog / step): axisN_set_rate_arcsec_per_sec, axisN_stop_rate,
        axisN_add_arcsec, axisN_reset. Only moves a TRACKING mount (P10)."""
        return await self._call("/mount/offset", **params)

    async def goto_ra_dec_j2000(self, ra_hours: float, dec_degs: float) -> Any:
        return await self._call("/mount/goto_ra_dec_j2000", ra_hours=ra_hours, dec_degs=dec_degs)

    async def set_axis0_wrap_range_min(self, degs: float) -> Any:
        return await self._call("/mount/set_axis0_wrap_range_min", degs=degs)

    # --- T3.5c custom path (D-SUN route): new -> add_point_list (POST) -> apply

    async def custom_path_new(self, coord: str = "altaz") -> Any:
        return await self._call("/mount/custom_path/new", PATH_WRITE_TIMEOUT_S, type=coord)

    async def custom_path_add_points(self, points: list[tuple[float, float, float]]) -> Any:
        """points = (absolute JD UTC, axis0/azimuth deg, altitude deg); form field `data` holds
        one "jd,c0,c1" line per point (same encoding as pwi4_client)."""
        body = "\n".join(f"{jd:.10f},{c0:.6f},{c1:.6f}" for jd, c0, c1 in points)
        try:
            resp = await asyncio.wait_for(
                self._http().post(
                    "/mount/custom_path/add_point_list",
                    data={"data": body},
                    timeout=httpx.Timeout(PATH_WRITE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                ),
                PATH_WRITE_TIMEOUT_S,
            )
        except TimeoutError:
            raise Pwi4Error("PWI4 did not answer in time") from None
        except httpx.HTTPError as exc:
            raise Pwi4Error(f"PWI4 not reachable ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise Pwi4Error(f"PWI4 refused the request (HTTP {resp.status_code})")
        return None

    async def custom_path_apply(self, update_wrap: int = 0) -> Any:
        """update_wrap=0 ALWAYS (P8): the gateway manages the wrap window itself."""
        return await self._call(
            "/mount/custom_path/apply", PATH_WRITE_TIMEOUT_S, update_wrap=int(update_wrap)
        )

    # --- T3.2 connection sequence (slow calls get a longer, still bounded, time limit)

    async def mount_connect(self) -> Any:
        return await self._call("/mount/connect", SLOW_WRITE_TIMEOUT_S)

    async def mount_disconnect(self) -> Any:
        return await self._call("/mount/disconnect", SLOW_WRITE_TIMEOUT_S)

    async def mount_enable(self, axis: int) -> Any:
        return await self._call("/mount/enable", SLOW_WRITE_TIMEOUT_S, axis=axis)

    async def mount_disable(self, axis: int) -> Any:
        return await self._call("/mount/disable", SLOW_WRITE_TIMEOUT_S, axis=axis)

    async def find_home(self) -> Any:
        return await self._call("/mount/find_home", SLOW_WRITE_TIMEOUT_S)


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
    # Dry run (POST /api/mount/check): the safety hook must not audit or store a plan, and
    # reports the target's observed (alt, az) in `target`.
    dry: bool = False
    target: tuple[float, float] | None = None
    code: str | None = None  # stable token of the refusal kind, set by the hook
    detour: dict[str, Any] | None = None  # T3.5c: {"duration_s", "side"} when D-SUN routes it


@dataclass
class MotionVerdict:
    """What a motion command would get: `reason` None = allowed. `code` is a stable machine
    token for the refusal kind (None when unclassified); `target` the observed (alt, az)."""

    reason: str | None
    code: str | None = None
    target: tuple[float, float] | None = None
    detour: dict[str, Any] | None = None  # T3.5c: the goto would be routed around the Sun


CheckHook = Callable[[Command, CommandContext], str | None]


def check_command(cmd: Command, ctx: CommandContext) -> str | None:
    """Safety seam. T3.1b replaces this with the real safety engine (limits, Sun, site gate).
    Return a refusal reason, or None to allow. T3.1a: nothing to add.
    """
    return None


@dataclass
class HomeVerdict:
    """Result of the HOME safety check (SafetyEngine.check_home): `reason` None = allowed."""

    reason: str | None = None
    sun_blocked: bool = False  # the refusal is the Sun disc (wording of the warning / question)
    start_sun_sep: float | None = None  # Sun separation at the start pose (recovery rule)
    route: Any = None  # T3.5c: a dsun.Route to the home pose (the path is Sun-blocked, Q10)


@dataclass
class ActiveRoute:
    """The D-SUN route being flown (snapshot `route`, the state override, cancel cleanup)."""

    sides: list[str]
    t_end_utc: datetime
    kind: str  # goto_altaz / goto_radec_j2000 / wrap_turn / home


def route_wrap_plan(
    points: list[dsun.RoutePoint],
) -> tuple[float, list[dsun.RoutePoint], bool]:
    """(wrap_min, points to fly, truncated) for a route (P8/P9/P21).

    The wrap window [w, w + 360) must hold every flown axis-0 value with >= 1 deg margin, or the
    mount unwinds ~346 deg mid-path. A route within ROUTE_WRAP_FIT_DEG fits entirely
    (w = lo - 1). A longer one (a 360 deg wrap_turn) cannot: the window is anchored at the start
    pose on the travel side and the points are cut at the first one outside it; the final
    command then finishes the last few degrees."""
    xs = [p.axis0 for p in points]
    lo, hi = min(xs), max(xs)
    if hi - lo <= ROUTE_WRAP_FIT_DEG:
        return lo - ROUTE_WRAP_MARGIN_DEG, list(points), False
    s = points[0].axis0
    up = (max(xs) - s) >= (s - min(xs))
    w = s - 2.0 if up else s + 2.0 - 359.0  # the start pose sits 2 deg inside the window
    kept: list[dsun.RoutePoint] = []
    for p in points:
        if not w + ROUTE_WRAP_MARGIN_DEG <= p.axis0 <= w + ROUTE_WRAP_FIT_DEG + 1.0:
            break
        kept.append(p)
    return w, kept, True


HomeCheck = Callable[[Any], HomeVerdict]


def home_check_allow(status: Any) -> HomeVerdict:
    """Default home check (no safety engine attached)."""
    return HomeVerdict()


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
        # T3.2: HOME safety check (SafetyEngine.check_home) and the homing watch of the monitor.
        self.home_check: HomeCheck = home_check_allow
        self.homing_begin: Callable[[HomeVerdict], None] = lambda verdict: None
        self.homing_end: Callable[[], None] = lambda: None
        # T3.5c D-SUN: the safety engine's stored route of a command, the monitor's route plan,
        # the check of the short final goto after a route, and the wait (a test seam).
        self.take_route: Callable[[Command], Any] = lambda cmd: None
        self.route_begin: Callable[..., None] = lambda *a, **k: None
        self.route_final_check: Callable[[Command], str | None] = lambda cmd: None
        self.route_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
        self.route: ActiveRoute | None = None
        self._route_set_override = False
        self._route_prev: tuple[str, str | None] | None = None
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
        self._startup_deferred = False  # PWI4 was unreachable at gateway start (CONNECT may run)
        self._startup_lock = asyncio.Lock()
        from .sequences import Sequencer  # local: sequences imports this module

        self.seq = Sequencer(self)
        from .jog import JogController  # local: jog imports this module

        self.jogc = JogController(self)
        self.homed = HomedGuard(audit)
        self.homed.connecting = lambda: (
            self.seq.active and self.seq.info is not None and self.seq.info.name == "connect"
        )
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
        self,
        conn_id: str,
        cmd_id: str | None,
        cmd: str | None,
        ok: bool,
        reason: str | None,
        code: str | None = None,
    ) -> dict[str, Any]:
        ack: dict[str, Any] = {
            "type": "ack",
            "id": cmd_id,
            "cmd": cmd,
            "ok": ok,
            "reason": reason,
            "control": self.control_for(conn_id),
        }
        if code is not None:  # machine-readable refusal kind (e.g. home_blocked_confirm)
            ack["code"] = code
        return ack

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
            await self.jogc.release("release")  # a jog needs its keep-alive sender to hold control
            self._set_holder(None, "release_control")
            self._audit_cmd(conn_id, cmd, True, None)
            return self._ack(conn_id, cmd.id, name, True, None)

        if name in SEQUENCE_COMMANDS:
            return self._sequence_command(conn_id, cmd)
        if name == "homed_override":
            return self._homed_override(conn_id, cmd)
        if name in ("jog", "jog_stop", "step", "wrap_turn", "wrap_turn_cancel"):
            return await self.jogc.handle(conn_id, cmd)

        if name == "track_object":  # T4.1: validated, the verdict is /api/mount/check; T4.2 runs it
            return self._refuse(conn_id, cmd, R_TRACK_NA, "not_available")

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

    def _refuse(
        self, conn_id: str, cmd: Command, reason: str, code: str | None = None
    ) -> dict[str, Any]:
        self._audit_cmd(conn_id, cmd, False, reason, **({"code": code} if code else {}))
        return self._ack(conn_id, cmd.id, cmd.name, False, reason, code)

    # ------------------------------------------------------------------ not-homed guard (T3.3)

    def _homed_override(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        """Proceed without homing. Needs the control lock like a motion command (taken when
        free); only while connected and not homed. No PWI4 write."""
        if self.shutting_down:
            return self._refuse(conn_id, cmd, R_SHUTTING)
        if not self.ready:
            return self._refuse(
                conn_id, cmd, R_STARTING if self.poller.last_good is not None else R_NO_PWI4
            )
        if self.holder is not None and self.holder != conn_id:
            return self._refuse(conn_id, cmd, R_OTHER_HAS_CONTROL)
        if self.seq.active:
            return self._refuse(conn_id, cmd, self.seq.busy_reason())
        status = self.fresh_status()
        if status is None:
            return self._refuse(conn_id, cmd, R_NO_PWI4)
        if not status.mount.is_connected:
            return self._refuse(conn_id, cmd, R_NO_MOUNT)
        if self.homed.stalled:
            return self._refuse(conn_id, cmd, R_STALLED)  # T3.3b: only a completed home helps
        if not self.homed.blocking:
            return self._refuse(conn_id, cmd, R_NOT_HOMED_STATE)
        if self.holder is None:
            self._set_holder(conn_id, "first_command")
        self.homed.override(conn_id)
        self.hub.republish_latest()
        self._audit_cmd(conn_id, cmd, True, None)
        return self._ack(conn_id, cmd.id, cmd.name, True, None)

    # ------------------------------------------------------------------ sequences (T3.2)

    def _sequence_command(self, conn_id: str, cmd: Command) -> dict[str, Any]:
        """connect / disconnect / home: acked when STARTED; progress is in snapshot.sequence.
        No await happens before the sequence is registered (per-connection ordering)."""
        reason, code = self._sequence_refusal(conn_id, cmd)
        if reason:
            return self._refuse(conn_id, cmd, reason, code)
        if self.holder is None:
            self._set_holder(conn_id, "first_command")
        self.seq.start(cmd, conn_id)
        self._audit_cmd(conn_id, cmd, True, None)
        return self._ack(conn_id, cmd.id, cmd.name, True, None)

    def _sequence_refusal(self, conn_id: str, cmd: Command) -> tuple[str | None, str | None]:
        name = cmd.name
        if self.shutting_down:
            return R_SHUTTING, None
        if not self.ready and not (name == "connect" and self._startup_deferred):
            return (R_STARTING if self.poller.last_good is not None else R_NO_PWI4), None
        if self._link_busy:
            return R_LINK_STOP, None
        if self.holder is not None and self.holder != conn_id:
            return R_OTHER_HAS_CONTROL, None
        if self.seq.active:
            return self.seq.busy_reason(), None
        if self._current is not None or not self._queue.empty():
            return R_WORKER_BUSY, None
        if name == "connect":
            return None, None  # PWI4 may be down: the sequence starts it
        status = self.fresh_status()
        if status is None:
            return R_NO_PWI4, None
        if not status.mount.is_connected:
            return R_NO_MOUNT, None
        axes_on = all(ax.is_enabled for ax in (status.mount.axis0, status.mount.axis1))
        if name == "home":
            for i, ax in enumerate((status.mount.axis0, status.mount.axis1)):
                if not ax.is_enabled:
                    return f"Axis {i} disabled", None
            return self.home_check(status).reason, None
        # disconnect: a blocked home asks the user once (the second send carries skip_home)
        if axes_on and not cmd.args.get("skip_home"):
            verdict = self.home_check(status)
            if verdict.reason:
                if verdict.sun_blocked:
                    q = "Home is blocked by the Sun; disconnect without homing?"
                else:
                    q = f"Home is blocked ({verdict.reason}); disconnect without homing?"
                return q, CODE_HOME_BLOCKED
        return None, None

    def _motion_refusal(self, conn_id: str, cmd: Command) -> str | None:
        return self._motion_verdict(cmd, conn_id).reason

    def dry_check(self, cmd: Command) -> MotionVerdict:
        """The verdict `cmd` would get right now from any window that holds (or can take)
        control: the same rules as the real command, minus the lock-holder rule. Read-only."""
        v = self._motion_verdict(cmd, None)
        if v.target is None and cmd.name == "goto_radec_j2000":
            v.target = self._observed_target(cmd)  # the target is useful beside a refusal too
        elif cmd.name == "goto_altaz":
            v.target = (cmd.args["alt"], cmd.args["az"])
        return v

    def _observed_target(self, cmd: Command) -> tuple[float, float] | None:
        ephem = self.poller.ephem
        site = self.poller.pwi4_site()
        if site is None or not ephem.available:
            return None
        try:
            alt, az = ephem.radec_j2000_altaz(site, cmd.args["ra_h"], cmd.args["dec_deg"])
        except Exception:
            return None
        return (alt, az)

    def _motion_verdict(self, cmd: Command, conn_id: str | None) -> MotionVerdict:
        """THE verdict function of a motion command, shared by the real command (`conn_id` = the
        sender, also audited by the safety hook) and the dry run (`conn_id` None: the lock-holder
        rule is skipped and nothing is audited or planned)."""
        dry = conn_id is None
        code: str | None = None
        reason = self._gateway_refusal(cmd, conn_id)
        ctx = None
        if reason is None:
            snap = self.poller.latest
            ctx = CommandContext(
                conn_id or "", self.fresh_status(), snap, self.poller.current_site_check(), dry
            )
            reason = self.check(
                self.jogc.effective(cmd) if cmd.name in ("step", "wrap_turn") else cmd, ctx
            )
            code = getattr(ctx, "code", None)
        else:
            code = self._code_of(reason)
        return MotionVerdict(
            reason,
            code,
            ctx.target if ctx is not None else None,
            getattr(ctx, "detour", None) if reason is None else None,
        )

    def _code_of(self, reason: str) -> str | None:
        if reason in _REASON_CODES:
            return _REASON_CODES[reason]
        if reason.startswith("Axis ") and reason.endswith(" disabled"):
            return "axis_disabled"
        if reason == self.seq.busy_reason():
            return "sequence_busy"
        return None

    def _gateway_refusal(self, cmd: Command, conn_id: str | None) -> str | None:
        if self.shutting_down:
            return R_SHUTTING
        if not self.ready:
            return R_STARTING if self.poller.last_good is not None else R_NO_PWI4
        if self._link_busy:
            return R_LINK_STOP
        if conn_id is not None and self.holder is not None and self.holder != conn_id:
            return R_OTHER_HAS_CONTROL
        if self.seq.active:
            return self.seq.busy_reason()
        status = self.fresh_status()
        if status is None:
            return R_NO_PWI4
        if not status.mount.is_connected:
            return R_NO_MOUNT
        if self.route is not None and cmd.name in MOTION_COMMANDS:
            return R_ROUTING  # a D-SUN route is flying: only STOP is accepted
        if cmd.name in MOTION_COMMANDS:
            for i, ax in enumerate((status.mount.axis0, status.mount.axis1)):
                if not ax.is_enabled:
                    return f"Axis {i} disabled"
        if self.homed.blocking and (
            cmd.name in HOMED_GATED or (cmd.name == "tracking" and cmd.args.get("on"))
        ):
            return R_STALLED if self.homed.stalled else R_NOT_HOMED
        snap = self.poller.latest
        starts_tracking = cmd.name == "track_object" or (
            cmd.name == "tracking" and cmd.args.get("on")
        )
        if starts_tracking and snap is not None:
            if snap.state == "SLEWING":
                return R_SLEWING
        return self.jogc.refusal(cmd, status)

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
            rg = self.take_route(cmd) if cmd.name in ROUTABLE else None
            if rg is not None:
                await self._route_job(job, rg)
            elif cmd.name == "goto_altaz":
                self.motion_cleared()
                # P15: PWI4's goto_alt_az does not end tracking; a local Az/El goto must end
                # STOPPED (logic sheet A5c-iv), so tracking goes off first.
                await self.writer.tracking_off()
                await self._prepare_wrap(cmd.args["az"])
                await self.writer.goto_alt_az(cmd.args["alt"], cmd.args["az"])
                self.motion_commanded = True
                self.goto_started(cmd)
            elif cmd.name == "wrap_turn":
                # T3.4e: reposition to the SAME alt/az through the other representation of axis
                # 0: the wrap window is centred on the target representation so that PWI4's
                # goto_alt_az picks it. The args are the effective command (JogController).
                self.motion_cleared()
                await self.writer.tracking_off()
                await self.writer.set_axis0_wrap_range_min(cmd.args["target0"] - 180.0)
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
                    # T3.3b (P19): tracking_on is sent as is. PWI4 itself normalises axis 0 into
                    # its wrap window (an unwinding turn after a stopped wrap-changing goto); that
                    # turn is intended and lands on the same RA/Dec. The safety monitor still
                    # watches it (Lim EL / Sun).
                    await self.writer.tracking_on()
                    self.motion_commanded = True
                    self._audit_track_start(job.conn_id, cmd)
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

    # ------------------------------------------------------------------ D-SUN route (T3.5c)

    def route_info(self) -> RouteInfo | None:
        ar = self.route
        if ar is None:
            return None
        t_end = ar.t_end_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return RouteInfo(sides=ar.sides, t_end=t_end)

    async def fly_route(
        self,
        route: Any,
        *,
        conn_id: str,
        cmd_id: str | None,
        kind: str,
        start_sun_sep: float | None = None,
        started: Callable[[], None] | None = None,
    ) -> tuple[bool, Any]:
        """Fly a dsun.Route as a PWI4 custom alt/az path and wait for its end (+ settle).

        tracking_off -> wrap window around the route (route_wrap_plan) -> custom_path new / add /
        apply(update_wrap=0), first point = the CURRENT pose at now + LEAD, then the state override
        "Avoiding the Sun", the monitor's route plan and the audit. Returns (truncated, last
        flown RoutePoint). The caller sends its final command and MUST call `_route_end` (also on
        error / cancel). Raises Pwi4Error; cancellation propagates."""
        from . import dsun  # local: dsun imports safety, which imports this module

        st = self.fresh_status()
        m = st.mount if st is not None else None
        if m is None or m.axis0.position_degs is None or m.axis1.position_degs is None:
            raise Pwi4Error(R_NO_PWI4)
        pose = (m.axis0.position_degs, m.axis1.position_degs)
        p0 = route.points[0]
        if math.hypot(pose[0] - p0.axis0, pose[1] - p0.alt) > ROUTE_POSE_TOL_DEG:
            raise Pwi4Error("Mount moved since the check; try again")
        pts = [dsun.RoutePoint(0.0, pose[0] % 360.0, pose[1], pose[0]), *route.points[1:]]
        wrap_min, kept, truncated = route_wrap_plan(pts)
        if len(kept) < 2:
            raise Pwi4Error("Route does not fit the wrap window")
        last = kept[-1]
        total = dsun.LEAD_S + last.t + ROUTE_SETTLE_S
        self.motion_cleared()
        await self.writer.tracking_off()
        cur = m.axis0_wrap_range_min_degs
        if cur is None or abs(cur - wrap_min) >= 0.05:
            await self.writer.set_axis0_wrap_range_min(wrap_min)
        t0 = time.monotonic()
        now = datetime.now(UTC)
        self.route_begin(
            [(p.axis0, p.alt) for p in kept], (last.axis0, last.alt), total, start_sun_sep, pose
        )
        jd0 = jd_from_utc(now) + dsun.LEAD_S / 86400.0
        await self.writer.custom_path_new("altaz")
        await self.writer.custom_path_add_points([(jd0 + p.t / 86400.0, p.az, p.alt) for p in kept])
        await self.writer.custom_path_apply(0)
        self.motion_commanded = True
        self.route = ActiveRoute(list(route.sides), now + timedelta(seconds=total), kind)
        self._route_prev = state.get_override()
        self._route_set_override = self._route_prev is None or self._route_prev[0] != "BUSY"
        if self._route_set_override:
            state.set_override("SLEWING", ROUTE_REASON)
        self.audit.write(
            "sun_detour", client=conn_id, id=cmd_id, kind=kind,
            start=[round(pose[0], 3), round(pose[1], 3)],
            target=[round(route.points[-1].axis0, 3), round(route.points[-1].alt, 3)],
            sides=list(route.sides) or ["straight"], duration_s=round(route.duration_s, 1),
            detour_deg=round(route.detour_deg, 2), min_sun_sep=round(route.min_sun_sep_deg, 2),
            points=len(kept), truncated=truncated, wrap_min=round(wrap_min, 2),
        )  # fmt: skip
        self.hub.republish_latest()
        if started is not None:
            started()
        await self.route_sleep(max(0.0, total - (time.monotonic() - t0)))
        return truncated, last

    def _route_end(self, event: str, **fields: Any) -> None:
        """Leave the route: restore the state override, drop the snapshot route, audit.
        Idempotent (a no-op when no route is active)."""
        ar = self.route
        if ar is None:
            return
        self.route = None
        if self._route_set_override:
            prev = self._route_prev
            if prev is None:
                state.clear_override()
            else:
                state.set_override(*prev)
        self._route_set_override, self._route_prev = False, None
        self.audit.write(event, kind=ar.kind, **fields)
        self.hub.republish_latest()

    async def _route_job(self, job: _Job, route_goto: Any) -> None:
        """A routed goto_altaz / goto_radec_j2000 / wrap_turn: fly the route, then the command's
        own final move from where the route ended (which may be a truncated wrap_turn sweep)."""
        cmd = job.cmd
        route = route_goto.route

        def started() -> None:
            if not job.future.done():
                job.future.set_result((True, None))  # acked at the route start, not at its end

        try:
            await self.fly_route(
                route, conn_id=job.conn_id, cmd_id=cmd.id, kind=cmd.name,
                start_sun_sep=route_goto.start_sun_sep, started=started,
            )  # fmt: skip
            reason = self.route_final_check(cmd)  # the short final leg must be Sun-clear too
            if reason:
                raise Pwi4Error(reason)
            self.motion_cleared()
            if cmd.name == "goto_altaz":
                await self.writer.tracking_off()
                await self._prepare_wrap(cmd.args["az"])
                await self.writer.goto_alt_az(cmd.args["alt"], cmd.args["az"])
            elif cmd.name == "wrap_turn":
                await self.writer.tracking_off()
                await self.writer.set_axis0_wrap_range_min(cmd.args["target0"] - 180.0)
                await self.writer.goto_alt_az(cmd.args["alt"], cmd.args["az"])
            else:
                az = self.resolved_az(cmd)
                await self._prepare_wrap(az if az is not None else route.points[-1].az)
                await self.writer.goto_ra_dec_j2000(cmd.args["ra_h"], cmd.args["dec_deg"])
            self.motion_commanded = True
            self.goto_started(cmd)
            self._route_end("sun_detour_done", id=cmd.id)
            if not job.future.done():
                job.future.set_result((True, None))
        except Pwi4Error as exc:
            with contextlib.suppress(Pwi4Error):
                await self._stop_requests()
            self._route_end("sun_detour_stopped", id=cmd.id, reason=str(exc))
            raise
        finally:  # cancel (STOP / link loss / safety stop) or any bug: always leave cleanly
            self._route_end(
                "sun_detour_stopped", id=cmd.id, reason=job.cancelled_reason or "Cancelled"
            )

    async def fly_home_route(self, route: Any, conn_id: str | None, start_sun_sep: float | None):
        """HOME whose direct path is Sun-blocked, with a trusted pose (Q10): fly the D-SUN route
        to the home pose; the caller then runs find_home with the normal homing watch."""
        try:
            await self.fly_route(
                route, conn_id=conn_id or "", cmd_id=None, kind="home",
                start_sun_sep=start_sun_sep,
            )  # fmt: skip
            await self.writer.tracking_off()  # the path hold reports tracking
            self.motion_cleared()
            self._route_end("sun_detour_done")
        except Pwi4Error as exc:
            with contextlib.suppress(Pwi4Error):
                await self._stop_requests()
            self._route_end("sun_detour_stopped", reason=str(exc))
            raise
        finally:
            self._route_end("sun_detour_stopped", reason="Cancelled")

    def _audit_track_start(self, conn_id: str, cmd: Command) -> None:
        """Audit the pointing the track starts from (T3.3: TRACK holds the current RA/Dec)."""
        st = self.fresh_status()
        pos: dict[str, Any] = {}
        if st is not None:
            m = st.mount
            pos = {
                "ra_h": m.ra_j2000_hours,
                "dec_deg": m.dec_j2000_degs,
                "alt": m.altitude_degs,
                "az": m.azimuth_degs,
            }
        self.audit.write("track_start", client=conn_id, id=cmd.id, **pos)

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
        self.jogc.on_cancel(reason)
        if self.seq.active:  # connect / disconnect / home end with STOP, link loss, safety stop
            self.seq.cancel(reason)
            n += 1
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
        self._spawn(self.jogc.cleanup())  # a jog / step left rate offsets behind
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
            self._spawn(self.jogc.cleanup())

    async def stall_stop(self, axis: int, dist_arcsec: float, duration_s: float) -> None:
        """Stall guard (T3.3b, P19a): an axis commanded to move did not move. Stop like a safety
        stop and set the stalled not-homed guard (no motion until a completed home). The guard
        is set first, so it holds even if the stop cannot be delivered."""
        t0 = time.perf_counter()
        self.homed.stall(axis)
        self.hub.republish_latest()
        cancelled = self._cancel_all("Cancelled: mount stalled")
        try:
            info = await self._stop_requests()
            ok = True
        except Pwi4Error as exc:
            info, ok = {"error": str(exc)}, False
        self.audit.write(
            "stall_stop", axis=axis, dist_arcsec=round(dist_arcsec, 1),
            duration_s=round(duration_s, 2), delivered=ok, cancelled=cancelled,
            total_ms=round((time.perf_counter() - t0) * 1000.0, 1), **info,
        )  # fmt: skip
        if ok:
            self._spawn(self._confirm_after_stop("safety", "stall"))
            self._spawn(self.jogc.cleanup())

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
                    await self.jogc.cleanup()
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
            self._startup_deferred = True
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

    async def ensure_started(self, res: PollResult) -> bool:
        """CONNECT started PWI4 (or found it answering) while the start-up check was still
        deferred: run it now, before anything else touches the mount. True when ready."""
        if not self.ready:
            await self._startup_with(res)
        return self.ready

    async def _startup_with(self, res: PollResult) -> None:
        async with self._startup_lock:
            if self.ready:
                return
            await self._startup_locked(res)

    async def _startup_locked(self, res: PollResult) -> None:
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
