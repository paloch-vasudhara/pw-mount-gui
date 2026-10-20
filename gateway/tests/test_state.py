import pytest

from gateway import state
from gateway.state import MotionTracker, compute_state


def cs(**kw):
    base = dict(
        reachable=True,
        mount_connected=True,
        axis_enabled=(True, True),
        slewing_raw=False,
        tracking_raw=False,
        moving=False,
    )
    base.update(kw)
    return compute_state(**base)


def test_unreachable():
    s, r = cs(reachable=False)
    assert s == "DISCONNECTED" and "not reachable" in r


def test_mount_not_connected():
    assert cs(mount_connected=False) == ("DISCONNECTED", "mount not connected")


@pytest.mark.parametrize("axes,n", [((False, True), 0), ((True, False), 1)])
def test_axis_disabled_fault(axes, n):
    assert cs(axis_enabled=axes) == ("FAULT", f"axis {n} disabled")


def test_slewing_requires_motion():
    assert cs(slewing_raw=True, moving=True)[0] == "SLEWING"
    assert cs(slewing_raw=True, moving=False)[0] == "STOPPED"  # P2: flag lags a stop


def test_p3_slewing_beats_tracking():
    assert cs(slewing_raw=True, tracking_raw=True, moving=True)[0] == "SLEWING"
    assert cs(slewing_raw=False, tracking_raw=True)[0] == "TRACKING"


def test_stopped():
    assert cs() == ("STOPPED", None)


def test_override_hook():
    try:
        state.set_override("HOMING", "homing")
        assert cs() == ("HOMING", "homing")
        assert cs(reachable=False)[0] == "DISCONNECTED"
    finally:
        state.clear_override()
    assert cs()[0] == "STOPPED"


def test_derived_velocity():
    t = MotionTracker()
    t.update(0.0, 10.0, 5.0)
    v0, v1, moving = t.update(0.1, 11.0, 5.0)
    assert v0 == pytest.approx(10.0) and v1 == pytest.approx(0.0) and moving


def test_p2_frozen_positions_not_slewing_after_window():
    """is_slewing_raw stays true but positions freeze: not moving once 0.3 s have passed."""
    t = MotionTracker()
    t.update(0.0, 0.0, 0.0)
    assert t.update(0.1, 2.0, 0.0)[2] is True  # moving
    assert t.update(0.2, 2.0, 0.0)[2] is True  # still within 0.3 s of the last motion
    assert t.update(0.3, 2.0, 0.0)[2] is True
    assert t.update(0.4, 2.0, 0.0)[2] is False  # 0.3 s after last moving sample (0.1)
    assert cs(slewing_raw=True, moving=False)[0] == "STOPPED"


def test_sidereal_rate_is_not_motion():
    t = MotionTracker()
    for i in range(20):
        _, _, moving = t.update(i * 0.1, 0.004 * i * 0.1, 0.0)
        assert not moving


def test_gap_resets_velocity():
    t = MotionTracker()
    t.update(0.0, 0.0, 0.0)
    v0, _, moving = t.update(5.0, 90.0, 0.0)
    assert v0 == 0.0 and not moving
