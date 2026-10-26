"""Not-homed guard (T3.3, decisions Q3 / Q4): one `homed` state per mount connect session.

A session starts (state `not_homed`) when
- the gateway's CONNECT sequence connects the mount (`begin("connect_sequence")`), or
- the gateway observes a connected mount it did not connect itself: first observation after the
  gateway start, a PWI4 reconnect, or a disconnected -> connected change made elsewhere (PWI4's
  own GUI). It cannot know whether such a mount was homed (Q4).
The session ends (state None) when the mount is observed disconnected, or PWI4 has been
unreachable for UNREACHABLE_END_S.

`not_homed` is cleared to `homed` by a COMPLETED home (the simulator's no-op home counts; a
failed / timed-out / cancelled one does not) and to `overridden` by the `homed_override`
command; both stay until the next session. While `not_homed`, goto / tracking-on are refused
(control.py). Audit: homed_session_start (cause), homed (how), homed_override.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .audit import AuditLog
from .snapshot import HomedInfo

NOT_HOMED = "not_homed"
HOMED = "homed"
OVERRIDDEN = "overridden"
UNREACHABLE_END_S = 3.0  # PWI4 silent this long = the session is over (a blip is not)
R_NOT_HOMED = "Mount not homed"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class HomedGuard:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self._info: HomedInfo | None = None
        self._seen_any = False  # a reachable /status was observed since the gateway start
        self._was_reachable = False
        self._unreach_since: float | None = None
        # True while the CONNECT sequence runs: it starts the session itself (no "observed").
        self.connecting: Callable[[], bool] = lambda: False

    # -- state

    def info(self) -> HomedInfo | None:
        return self._info.model_copy() if self._info is not None else None

    @property
    def state(self) -> str | None:
        return self._info.state if self._info is not None else None

    @property
    def blocking(self) -> bool:
        return self._info is not None and self._info.state == NOT_HOMED

    # -- transitions

    def _set(self, state: str | None) -> None:
        self._info = HomedInfo(state=state, since=_now_iso()) if state else None

    def begin(self, cause: str) -> None:
        """Start a connect session (always not_homed)."""
        self._set(NOT_HOMED)
        self.audit.write("homed_session_start", cause=cause)

    def end(self, cause: str) -> None:
        if self._info is not None:
            self._set(None)
            self.audit.write("homed_session_end", cause=cause)

    def home_completed(self, how: str) -> None:
        """A home finished (`completed` or the sim's `no_motion`)."""
        if self._info is None:  # the mount is connected (it just homed): the session is open
            self.begin("observed_at_home")
        self._set(HOMED)
        self.audit.write("homed", how=how)

    def override(self, client: str | None = None) -> bool:
        """User chose to proceed without homing. Only valid while not_homed."""
        if not self.blocking:
            return False
        self._set(OVERRIDDEN)
        self.audit.write("homed_override", client=client)
        return True

    # -- poller feed

    def observe(self, result: Any, now: float) -> None:
        """Called after every fetch (reachable or not)."""
        if not result.reachable:
            if self._unreach_since is None:
                self._unreach_since = now
            elif now - self._unreach_since >= UNREACHABLE_END_S:
                self.end("pwi4_unreachable")
                self._was_reachable = False
            return
        self._unreach_since = None
        connected = bool(result.status.mount.is_connected)
        if not connected:
            self.end("mount_disconnected")
        elif self._info is None and not self.connecting():
            if not self._seen_any:
                cause = "gateway_start"
            elif not self._was_reachable:
                cause = "pwi4_reconnect"
            else:
                cause = "mount_connected_externally"
            self.begin(cause)
        self._seen_any = True
        self._was_reachable = True
