"""Connection and HOME sequences (T3.2, logic sheet A5a / A5c-i).

`connect`    start PWI4 (if /status is unreachable) -> mount/connect -> enable axes -> HOME
`disconnect` stop motion -> HOME -> disable axes -> mount/disconnect   (PWI4 stays open)
`home`       stop motion -> HOME                                       (state HOMING)

All PWI4 writes go through `Pwi4Writer` (control.py, invariant I2); this module issues none
itself. The only other side effect is *starting* the PWI4 executable (never stopping it).

State: connect / disconnect hold the BUSY override for the whole sequence, with
`state_reason` = the current step. A standalone `home` holds HOMING.
Failure (A5a): stop at the failing step, report "<step> failed: <reason>", stop motion and
disable both axes if the mount is connected, no rollback. STOP, link loss and safety stops
cancel a running sequence (status "cancelled"; nothing is rolled back either).
HOME completion: motion seen and then stationary for 1 s, or no motion within 3 s (the
simulator's find_home is a no-op, P12), or 180 s -> failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import state
from .commands import Command
from .control import MOVE_V_DEG_S, HomeVerdict, Pwi4Error
from .snapshot import SequenceInfo

if TYPE_CHECKING:
    from .control import ControlService

STEPS = {
    "connect": ["start PWI4", "connect mount", "enable axes", "home mount"],
    "disconnect": ["stop motion", "home mount", "disable axes", "disconnect mount"],
    "home": ["stop motion", "home mount"],
}
VERB = {"connect": "Connecting", "disconnect": "Disconnecting", "home": "Homing"}

PWI4_EXE_CANDIDATES = (
    r"C:\Program Files (x86)\PlaneWave Instruments\PlaneWave Interface 4\PWI4.exe",
    r"C:\Program Files\PlaneWave Instruments\PlaneWave Interface 4\PWI4.exe",
)
START_TIMEOUT_S = 60.0  # PWI4 start (may be lowered to 30 s later)
START_POLL_S = 0.5
CONNECT_WAIT_S = 30.0  # mount/connect -> status says connected
ENABLE_WAIT_S = 8.0
DISCONNECT_WAIT_S = 15.0
WAIT_POLL_S = 0.2
HOME_NO_MOTION_S = 3.0  # no motion within this long after find_home -> done (sim no-op, P12)
HOME_STILL_S = 1.0  # motion seen, then stationary this long -> done
HOME_TIMEOUT_S = 180.0
HOME_POLL_S = 0.1
STOP_CONFIRM_S = 3.0
SETTLE_MAX_S = 0.6  # wait for one fresh poll after the last step before BUSY is released


class StepError(Exception):
    """A sequence step failed; the message is the reason shown to the user."""


# ------------------------------------------------------------------ PWI4 executable


def find_pwi4_exe(override: str | None = None) -> str | None:
    """`PW_PWI4_EXE` / configured path first, then the standard install locations."""
    cands = ([override] if override else []) + list(PWI4_EXE_CANDIDATES)
    for c in cands:
        if c and Path(c).is_file():
            return c
    return None


def pwi4_process_running() -> bool:
    """True when a PWI4.exe process exists (read-only `tasklist`); False on any doubt."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq PWI4.exe", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "pwi4.exe" in out.lower()


def launch_pwi4(exe_override: str | None = None) -> dict[str, Any]:
    """Start PWI4 detached (it must outlive the gateway). Never stops or kills anything.

    A PWI4.exe that is already running (e.g. still starting up) is not launched a second
    time; the caller just keeps polling /status.
    """
    exe = find_pwi4_exe(exe_override)
    if exe is None:
        raise StepError("PWI4 executable not found (set PW_PWI4_EXE)")
    if pwi4_process_running():
        return {"exe": exe, "launched": False, "note": "PWI4.exe already running"}
    flags = 0
    if sys.platform == "win32":
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    proc = subprocess.Popen(
        [exe],
        cwd=str(Path(exe).parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    return {"exe": exe, "launched": True, "pid": proc.pid}


# ------------------------------------------------------------------ the runner


class Sequencer:
    def __init__(self, control: ControlService) -> None:
        self.control = control
        self.exe: str | None = None  # PW_PWI4_EXE override
        self.start_timeout = START_TIMEOUT_S
        # Seams for the unit tests (fake launcher, injected clock / sleep).
        self.launcher: Callable[[], dict[str, Any]] = lambda: launch_pwi4(self.exe)
        self.clock: Callable[[], float] = time.monotonic
        self.sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
        self.info: SequenceInfo | None = None
        self._task: asyncio.Task | None = None
        self._skip_home = False
        self._cancel_reason: str | None = None
        self._conn: str | None = None

    # -- public

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot_info(self) -> SequenceInfo | None:
        return self.info.model_copy() if self.info is not None else None

    def busy_reason(self) -> str:
        name = self.info.name if self.info else "sequence"
        return {"connect": "Busy: connecting", "disconnect": "Busy: disconnecting"}.get(
            name, "Busy: homing"
        )

    def start(self, cmd: Command, conn_id: str) -> None:
        name = cmd.name
        self._skip_home = bool(cmd.args.get("skip_home"))
        self._cancel_reason = None
        self._conn = conn_id
        self.info = SequenceInfo(name=name, steps=list(STEPS[name]), status="running")
        self.control.audit.write("sequence_start", client=conn_id, name=name, args=cmd.args)
        self._task = asyncio.create_task(self._run(name))

    def cancel(self, reason: str) -> None:
        if self.active and self._task is not None:
            self._cancel_reason = reason
            self._task.cancel()

    # -- bookkeeping

    def _audit(self, event: str, **fields: Any) -> None:
        name = self.info.name if self.info else None
        self.control.audit.write(event, client=self._conn, name=name, **fields)

    def _enter(self, idx: int) -> None:
        info = self.info
        assert info is not None
        info.step_index = idx
        info.step = info.steps[idx]
        reason = f"{VERB[info.name]}: {info.step}…"
        # connect / disconnect are BUSY; a standalone home is HOMING (logic sheet G8)
        state.set_override("HOMING" if info.name == "home" else "BUSY", reason)
        self._audit("sequence_step", step=info.step, index=idx)
        self.control.hub.republish_latest()

    def _warn(self, text: str, event: str, **fields: Any) -> None:
        assert self.info is not None
        self.info.warning = text
        self._audit(event, warning=text, **fields)

    async def _run(self, name: str) -> None:
        info = self.info
        assert info is not None
        # The final status is published only AFTER the make-safe tail and the poller settle, at
        # the same instant BUSY / HOMING is released: "running" always means "BUSY", so a client
        # that sees the sequence end may send its next command at once.
        outcome, error, cancelled = "done", None, False
        try:
            if name == "connect":
                await self._connect()
            elif name == "disconnect":
                await self._disconnect()
            else:
                await self._home_command()
            self._audit("sequence_done", warning=info.warning)
        except asyncio.CancelledError:
            cancelled = True
            info.status = "cancelled"
            info.error = self._cancel_reason or "Cancelled"
            self._audit("sequence_cancelled", step=info.step, reason=info.error)
        except (StepError, Pwi4Error) as exc:
            outcome, error = "failed", f"{_cap(info.step or name)} failed: {exc}"
            self._audit("sequence_failed", step=info.step, reason=str(exc))
            await self._make_safe()
        except Exception as exc:  # a bug must still leave the mount safe and say so
            outcome = "failed"
            error = f"{_cap(info.step or name)} failed: unexpected error ({type(exc).__name__})"
            self._audit("sequence_failed", step=info.step, reason=repr(exc))
            await self._make_safe()
        finally:
            self.control.homing_end()
            if not cancelled:
                await self._settle_poller()
                info.status, info.error = outcome, error
            state.clear_override()
            self.control.hub.republish_latest()

    async def _settle_poller(self) -> None:
        """Keep BUSY / HOMING until the 10 Hz poller has read PWI4 once after the last step, so
        the dashboard never flashes the stale pre-step state (e.g. FAULT 'axis disabled' right
        after a disconnect). Bounded; never raises."""
        poller = self.control.poller
        t0 = time.monotonic()
        try:
            while poller.last_good_t <= t0 and time.monotonic() - t0 < SETTLE_MAX_S:
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass

    async def _make_safe(self) -> None:
        """Failure rule (A5a): stop motion and disable both axes if the mount is connected.
        Best effort, no rollback of the earlier steps."""
        w = self.control.writer
        out: dict[str, Any] = {}
        try:
            res = await w.read_status()
            if res.reachable and res.status.mount.is_connected:
                with contextlib.suppress(Pwi4Error):
                    await w.stop()
                with contextlib.suppress(Pwi4Error):
                    await w.tracking_off()
                for ax in (0, 1):
                    try:
                        await w.mount_disable(ax)
                    except Pwi4Error as exc:
                        out[f"disable_{ax}_error"] = str(exc)
                out["made_safe"] = True
            else:
                out["made_safe"] = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            out["error"] = repr(exc)
        self._audit("sequence_safe", **out)

    # -- waiting helpers

    async def _wait_status(self, pred: Callable[[Any], bool], timeout_s: float, what: str) -> Any:
        """Poll PWI4 (fresh GETs) until `pred(status)`; StepError(what) on timeout."""
        t_end = self.clock() + timeout_s
        while True:
            res = await self.control.writer.read_status()
            if res.reachable and pred(res.status):
                return res.status
            if self.clock() >= t_end:
                raise StepError(what)
            await self.sleep(WAIT_POLL_S)

    async def _fresh(self) -> Any:
        res = await self.control.writer.read_status()
        if not res.reachable:
            raise StepError("PWI4 not reachable")
        return res.status

    # -- connect

    async def _start_pwi4(self) -> Any:
        """Launch PWI4 and wait (<= start_timeout) until /status answers."""
        try:
            info = await asyncio.to_thread(self.launcher)
        except StepError:
            raise
        except Exception as exc:
            raise StepError(f"could not start PWI4 ({type(exc).__name__}: {exc})") from None
        self._audit("pwi4_launch", **info)
        t_end = self.clock() + self.start_timeout
        while True:
            res = await self.control.writer.read_status()
            if res.reachable:
                return res
            if self.clock() >= t_end:
                raise StepError(f"PWI4 did not answer within {self.start_timeout:g} s")
            await self.sleep(START_POLL_S)

    async def _connect(self) -> None:
        c = self.control
        w = c.writer
        # 1. PWI4 running?
        self._enter(0)
        res = await w.read_status()
        if not res.reachable:
            res = await self._start_pwi4()
        if not await c.ensure_started(res):
            raise StepError("could not confirm that the mount is stopped")
        # 2. mount connected?
        self._enter(1)
        st = res.status
        if not st.mount.is_connected:
            await w.mount_connect()
            st = await self._wait_status(
                lambda s: bool(s.mount.is_connected), CONNECT_WAIT_S, "the mount did not connect"
            )
            c.homed.begin("connect_sequence")  # T3.3: a new connect session, not homed yet
        elif c.homed.state is None:
            c.homed.begin("already_connected")  # found connected, never observed: not homed (Q4)
        # 3. axes enabled?
        self._enter(2)
        for i, ax in enumerate((st.mount.axis0, st.mount.axis1)):
            if not ax.is_enabled:
                await w.mount_enable(i)
        st = await self._wait_status(
            lambda s: bool(s.mount.axis0.is_enabled and s.mount.axis1.is_enabled),
            ENABLE_WAIT_S,
            "the axes did not enable",
        )
        # 4. home (Sun-checked; skipped with a warning when blocked)
        self._enter(3)
        st = await self._fresh()
        verdict = c.home_check(st)
        if verdict.reason:
            self._warn(
                _skip_text(verdict), "home_skipped", reason=verdict.reason, sun=verdict.sun_blocked
            )
            return
        await self._home(verdict, stop_first=True)

    # -- disconnect

    async def _disconnect(self) -> None:
        c = self.control
        w = c.writer
        self._enter(0)
        await self._stop_motion()
        self._enter(1)
        st = await self._fresh()
        if not (st.mount.axis0.is_enabled and st.mount.axis1.is_enabled):
            self._warn("Home skipped: axes are disabled", "home_skipped", reason="axes disabled")
        else:
            verdict = c.home_check(st)
            if verdict.reason:
                if not self._skip_home:
                    raise StepError(verdict.reason)
                self._warn(
                    _skip_text(verdict) + " (disconnected without homing)",
                    "disconnect_without_home",
                    reason=verdict.reason,
                    sun=verdict.sun_blocked,
                )
            else:
                await self._home(verdict, stop_first=False)
        self._enter(2)
        st = await self._fresh()
        for i, ax in enumerate((st.mount.axis0, st.mount.axis1)):
            if ax.is_enabled:
                await w.mount_disable(i)
        await self._wait_status(
            lambda s: not (s.mount.axis0.is_enabled or s.mount.axis1.is_enabled),
            ENABLE_WAIT_S,
            "the axes did not disable",
        )
        self._enter(3)
        await w.mount_disconnect()
        await self._wait_status(
            lambda s: not s.mount.is_connected, DISCONNECT_WAIT_S, "the mount did not disconnect"
        )

    # -- standalone home

    async def _home_command(self) -> None:
        self._enter(0)
        await self._stop_motion()
        self._enter(1)
        st = await self._fresh()
        verdict = self.control.home_check(st)
        if verdict.reason:
            raise StepError(verdict.reason)
        await self._home(verdict, stop_first=False)

    # -- shared pieces

    async def _stop_motion(self) -> None:
        """Stop the mount and confirm it stands still (position-based, P2)."""
        c = self.control
        c.motion_cleared()
        await c._stop_requests()
        conf = await c._confirm_stopped(STOP_CONFIRM_S)
        if not conf.get("confirmed"):
            raise StepError("could not confirm that the mount stopped")

    async def _home(self, verdict: HomeVerdict, stop_first: bool) -> None:
        """HOME proper: (stop), re-check the Sun path, arm the homing watch, find_home, wait."""
        c = self.control
        if stop_first:
            await self._stop_motion()
        st = await self._fresh()
        v = c.home_check(st)  # the pose may have changed since the caller's check
        if v.reason:
            raise StepError(v.reason)
        c.homing_begin(v)  # monitor: actual-limit watch only (the real path is unknown, P12)
        try:
            await c.writer.find_home()
            # Until a stop or a completed home says otherwise the mount may be moving: a lost
            # control link must stop it even if the status has not shown motion yet (G4).
            c.motion_commanded = True
            how = await self._wait_home_done()
            c.motion_commanded = False  # stationary for 1 s (or no motion at all)
            c.homed.home_completed(how)  # T3.3: only a completed home clears the guard
        finally:
            c.homing_end()
        self._audit("home_finished", how=how)

    async def _wait_home_done(self) -> str:
        """motion seen then still for 1 s -> "completed"; no motion within 3 s -> "no_motion"
        (the simulator, P12); 180 s -> StepError."""
        w = self.control.writer
        t0 = self.clock()
        last: tuple[float, float, float] | None = None
        moved = False
        still_since: float | None = None
        while True:
            res = await w.read_status()
            now = self.clock()
            if res.reachable:
                m = res.status.mount
                if not m.is_connected:
                    raise StepError("the mount disconnected while homing")
                p0, p1 = m.axis0.position_degs, m.axis1.position_degs
                if p0 is not None and p1 is not None:
                    if last is not None and now - last[0] > 0.02:
                        v = max(abs(p0 - last[1]), abs(p1 - last[2])) / (now - last[0])
                        if v > MOVE_V_DEG_S:
                            moved, still_since = True, None
                        elif moved and still_since is None:
                            still_since = now
                    last = (now, p0, p1)
            if moved and still_since is not None and now - still_since >= HOME_STILL_S:
                return "completed"
            if not moved and now - t0 >= HOME_NO_MOTION_S:
                return "no_motion"
            if now - t0 >= HOME_TIMEOUT_S:
                raise StepError(f"homing did not finish within {HOME_TIMEOUT_S:g} s")
            await self.sleep(HOME_POLL_S)


def _cap(label: str) -> str:
    """Capitalise the first letter only ("start PWI4" -> "Start PWI4")."""
    return label[:1].upper() + label[1:]


def _skip_text(v: HomeVerdict) -> str:
    if v.sun_blocked:
        return "Home skipped: path passes within 15° of the Sun"
    return f"Home skipped: {v.reason}"
