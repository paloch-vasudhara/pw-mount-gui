"""G8 mount state (pure functions + motion tracker + override hook).

P2: PWI4's is_slewing can lag a stop by up to ~1.65 s, so "moving" is judged from
positions: derived axis velocity > MOVE_V_DEG_S in any sample within MOVE_WINDOW_S.
"""

from __future__ import annotations

from collections import deque

MOVE_V_DEG_S = 0.05
MOVE_WINDOW_S = 0.3
MIN_DT_S = 0.02  # samples closer than this reuse the previous velocity
MAX_GAP_S = 1.0  # after a longer gap velocity restarts from 0


class MotionTracker:
    """Derives axis velocities from successive positions and says whether the mount moves."""

    def __init__(self) -> None:
        self._last: tuple[float, float, float] | None = None  # t, p0, p1
        self._vel: tuple[float, float] = (0.0, 0.0)
        self._moving_samples: deque[float] = deque()  # times of samples with motion

    def reset(self) -> None:
        self._last = None
        self._vel = (0.0, 0.0)
        self._moving_samples.clear()

    def update(self, t: float, p0: float | None, p1: float | None) -> tuple[float, float, bool]:
        """Feed one sample (monotonic seconds). Returns (v0, v1, moving)."""
        if p0 is None or p1 is None:
            self.reset()
            return 0.0, 0.0, False
        if self._last is None or t - self._last[0] > MAX_GAP_S:
            self._vel = (0.0, 0.0)
            self._last = (t, p0, p1)
        else:
            dt = t - self._last[0]
            if dt >= MIN_DT_S:
                self._vel = ((p0 - self._last[1]) / dt, (p1 - self._last[2]) / dt)
                self._last = (t, p0, p1)
        if max(abs(self._vel[0]), abs(self._vel[1])) > MOVE_V_DEG_S:
            self._moving_samples.append(t)
        while self._moving_samples and self._moving_samples[0] < t - MOVE_WINDOW_S:
            self._moving_samples.popleft()
        return self._vel[0], self._vel[1], bool(self._moving_samples)

    def moving_at(self, t: float) -> bool:
        """Re-evaluate motion at time t without a new sample (publisher ticks)."""
        return any(s >= t - MOVE_WINDOW_S for s in self._moving_samples)


_override: tuple[str, str | None] | None = None


def set_override(state: str, reason: str | None = None) -> None:
    """Command-layer hook (T3.2): BUSY = connect/disconnect sequence (wins over everything, the
    mount may not even be connected yet); HOMING = a home run (needs a live, enabled mount)."""
    global _override
    _override = (state, reason)


def get_override() -> tuple[str, str | None] | None:
    return _override


def clear_override() -> None:
    global _override
    _override = None


def compute_state(
    *,
    reachable: bool,
    mount_connected: bool | None,
    axis_enabled: tuple[bool | None, bool | None],
    slewing_raw: bool | None,
    tracking_raw: bool | None,
    moving: bool,
    unreachable_reason: str = "PWI4 not running / not reachable",
) -> tuple[str, str | None]:
    # BUSY (connect/disconnect sequence, A5a) covers the whole sequence, including the steps where
    # PWI4 is not running yet, the mount is not connected or the axes are disabled.
    if _override is not None and _override[0] == "BUSY":
        return _override
    if not reachable:
        return "DISCONNECTED", unreachable_reason
    if not mount_connected:
        return "DISCONNECTED", "mount not connected"
    for i, en in enumerate(axis_enabled):
        if not en:
            return "FAULT", f"axis {i} disabled"
    if _override is not None:
        return _override
    if slewing_raw and moving:
        return "SLEWING", None
    if tracking_raw:
        return "TRACKING", None
    return "STOPPED", None
