"""T3.3b (P19a): stall guard. Pure StallWatch tests, the homed-guard transitions, and the monitor
against FakePwi4 (stop + audit + snapshot + refusals). The simulator reproduction is in
test_stall_pwi4sim.py.
"""

from __future__ import annotations

import math
import threading
import time
from types import SimpleNamespace

from gateway.audit import AuditLog
from gateway.homed import HomedGuard
from gateway.safety import (
    STALL_CMD_DIST_ARCSEC,
    STALL_POS_TOL_ARCSEC,
    STALL_S,
    StallWatch,
)

from .conftest import FakePwi4
from .test_control import Win, audit_lines, running, wait_for
from .test_homed import events, homed_of, homed_state
from .test_sequences import home_fast, wait_seq

STALLED = "Mount stalled; home it first"
WARNING = (
    "Mount stalled (axis {n} not moving); it may be at a physical limit. "
    "Home the mount before further use."
)


# ------------------------------------------------------------------ pure detector


def axis(pos=100.0, dist=0.0, tgt=None, sv=None):
    return SimpleNamespace(
        position_degs=pos,
        dist_to_target_arcsec=dist,
        target_mech_position_degs=tgt,
        setpoint_velocity_degs_per_sec=sv,
    )


def mount(a0, a1):
    return SimpleNamespace(axis0=a0, axis1=a1)


def feed(w: StallWatch, samples, dt=0.1, t0=0.0, active=True):
    """samples: callable(t) -> mount. Returns the first event and its time."""
    t = t0
    for _ in range(int(60.0 / dt)):
        ev = w.update(t, samples(t - t0), active)
        if ev is not None:
            return ev, t - t0
        t += dt
    return None, None


def test_az_axis_stall_fires_after_2p5_s_with_axis_and_distance():
    w = StallWatch()
    ev, t = feed(w, lambda t: mount(axis(100.0, 90000.0), axis(45.0, 0.0)))
    assert ev is not None and ev.axis == 0 and ev.dist_arcsec == 90000.0
    assert STALL_S <= t <= STALL_S + 0.15
    assert ev.duration_s >= STALL_S


def test_alt_axis_stall_is_detected_too():
    w = StallWatch()
    ev, t = feed(w, lambda t: mount(axis(100.0, 0.0), axis(-40.0, -30000.0)))
    assert ev is not None and ev.axis == 1 and ev.dist_arcsec == 30000.0
    assert STALL_S <= t <= STALL_S + 0.15


def test_alt_reaching_its_target_first_and_sitting_still_does_not_trigger():
    """The altitude axis arrives early (its own target difference ~0) and stays put while the
    azimuth axis keeps slewing for 20 s: no stall."""
    w = StallWatch()

    def s(t):
        return mount(axis(100.0 + 8.0 * t, 160000.0 - 8.0 * 3600 * t), axis(45.0, 0.2))

    ev, _ = feed(w, s)
    assert ev is None


def test_alt_settling_exponentially_never_looks_stalled():
    w = StallWatch()

    def s(t):
        d = 20.0 * math.exp(-t / 0.5)  # degrees to go, tau 0.5 s (P13)
        return mount(axis(100.0, 0.0), axis(45.0 - d, d * 3600.0))

    ev, _ = feed(w, s)
    assert ev is None  # moving while commanded, then below the command threshold


def test_sidereal_tracking_never_triggers_at_any_pose():
    """Healthy tracking: follow error of a few arcsec, positions creeping at 0 .. 15 arcsec/s
    (the az rate near the meridian / zenith can be ~0): the axis is never 'commanded'."""
    for rate_az, rate_alt in [(0.0, 0.0), (0.3, 0.0), (4.0, 12.0), (-9.0, 0.0), (400.0, 0.1)]:
        w = StallWatch()

        def s(t, ra=rate_az, rl=rate_alt):
            return mount(axis(150.0 + ra * t / 3600.0, 3.0), axis(60.0 + rl * t / 3600.0, -2.0))

        ev, _ = feed(w, s)
        assert ev is None, (rate_az, rate_alt)


def test_slow_creep_below_the_position_tolerance_still_counts_as_stalled():
    w = StallWatch()
    # 0.2 arcsec/s: 0.5 arcsec in 2.5 s <= 1 arcsec
    ev, t = feed(w, lambda t: mount(axis(100.0 + 0.2 * t / 3600.0, 50000.0), axis(45.0, 0.0)))
    assert ev is not None and ev.axis == 0 and t <= STALL_S + 0.15


def test_motion_above_the_position_tolerance_restarts_the_window():
    w = StallWatch()
    # 1.5 arcsec every 2 s: never 2.5 s within 1 arcsec of the reference
    steps = lambda t: mount(  # noqa: E731
        axis(100.0 + 1.5 * (t // 2.0) / 3600.0, 50000.0), axis(45.0, 0.0)
    )
    ev, _ = feed(w, steps)
    assert ev is None
    assert STALL_POS_TOL_ARCSEC == 1.0


def test_command_distance_threshold():
    w = StallWatch()
    below = STALL_CMD_DIST_ARCSEC - 1.0
    ev, _ = feed(w, lambda t: mount(axis(100.0, below), axis(45.0, 0.0)))
    assert ev is None
    w = StallWatch()
    ev, _ = feed(w, lambda t: mount(axis(100.0, -(STALL_CMD_DIST_ARCSEC + 1.0)), axis(45.0, 0.0)))
    assert ev is not None and ev.axis == 0  # the sign does not matter


def test_target_mech_difference_is_used_when_dist_is_not_reported():
    w = StallWatch()
    ev, _ = feed(w, lambda t: mount(axis(-40.0, None, tgt=-60.0), axis(45.0, 0.0)))
    assert ev is not None and ev.axis == 0
    assert math.isclose(ev.dist_arcsec, 20.0 * 3600.0)


def test_setpoint_velocity_counts_as_commanded_on_a_real_mount():
    w = StallWatch()
    ev, _ = feed(w, lambda t: mount(axis(100.0, 0.0, sv=0.0), axis(45.0, 0.0, sv=0.5)))
    assert ev is not None and ev.axis == 1
    w = StallWatch()  # a sidereal setpoint (0.004 deg/s) is not 'commanded'
    ev, _ = feed(w, lambda t: mount(axis(100.0, 0.0, sv=0.004), axis(45.0, 0.0, sv=0.004)))
    assert ev is None


def test_inactive_mount_never_triggers_and_resets_the_window():
    w = StallWatch()
    ev, _ = feed(w, lambda t: mount(axis(100.0, 90000.0), axis(45.0, 0.0)), active=False)
    assert ev is None
    # becoming active starts a fresh window
    ev, t = feed(w, lambda t: mount(axis(100.0, 90000.0), axis(45.0, 0.0)), t0=100.0)
    assert ev is not None and t >= STALL_S


def test_reset_restarts_the_window():
    w = StallWatch()
    m = mount(axis(100.0, 90000.0), axis(45.0, 0.0))
    for i in range(20):
        assert w.update(i * 0.1, m, True) is None
    w.reset()
    assert w.update(2.1, m, True) is None
    assert w.update(4.0, m, True) is None
    assert w.update(4.7, m, True) is not None


def test_missing_position_is_ignored():
    w = StallWatch()
    ev, _ = feed(w, lambda t: mount(axis(None, 90000.0), axis(None, 0.0)))
    assert ev is None


# ------------------------------------------------------------------ homed guard transitions


def make_guard(tmp_path) -> HomedGuard:
    return HomedGuard(AuditLog(tmp_path / "audit"))


def test_guard_stalled_transitions(tmp_path):
    g = make_guard(tmp_path)
    g.begin("connect_sequence")
    assert g.info().reason is None and not g.stalled
    g.stall(1)
    info = g.info()
    assert info.state == "not_homed" and info.reason == "stalled"
    assert g.blocking and g.stalled
    assert g.stall_message() == WARNING.format(n=1)
    assert g.override("w1") is False  # no override while stalled
    assert g.state == "not_homed"
    g.home_completed("completed")
    assert g.info().state == "homed" and g.info().reason is None and not g.stalled
    assert g.stall_message() is None
    g.stall(0)
    g.end("mount_disconnected")
    assert g.info() is None
    g.begin("connect_sequence")  # a new session is a normal not_homed
    assert g.info().state == "not_homed" and g.info().reason is None
    assert g.stall_message() is None
    assert g.override("w1") is True


def test_guard_stall_after_override_and_without_session(tmp_path):
    g = make_guard(tmp_path)
    g.stall(0)  # no session yet (mount moved before it was observed): opens one
    assert g.stalled
    g2 = make_guard(tmp_path)
    g2.begin("x")
    g2.override("w")
    assert g2.state == "overridden"
    g2.stall(1)
    assert g2.info().state == "not_homed" and g2.info().reason == "stalled"


# ------------------------------------------------------------------ monitor on FakePwi4


def stall_fake(axis_no: int = 0) -> FakePwi4:
    """A fake whose motion is switched on by `begin_stall` once the gateway is running (a mount
    found moving at start-up would be stopped by the start-up stop instead)."""
    fake = FakePwi4()
    fake.stall_axis = axis_no  # type: ignore[attr-defined]
    return fake


def begin_stall(fake: FakePwi4) -> None:
    fake.set(
        mount__is_slewing=True,
        **{f"mount__axis{fake.stall_axis}__dist_to_target_arcsec": 120000.0},  # type: ignore[attr-defined]
    )


def test_monitor_stops_a_stalled_axis_sets_the_guard_and_refuses_motion(tmp_path):
    fake = stall_fake(0)
    with running(fake) as (tc, _, app):
        t0 = time.monotonic()
        begin_stall(fake)
        wait_for(lambda: any(p == "/mount/stop" for p, _ in fake.writes), timeout=8)
        took = time.monotonic() - t0
        assert 2.2 <= took <= 4.5, took
        wait_for(lambda: homed_of(app) and homed_of(app)["reason"] == "stalled")
        assert homed_state(app) == "not_homed"
        wait_for(lambda: app.state.poller.latest.state == "STOPPED")
        snap = app.state.poller.latest
        assert snap.state_reason == WARNING.format(n=0)
        assert tc.get("/api/mount/status").json()["homed"]["reason"] == "stalled"
        assert app.state.monitor.stalls == 1
        w = Win(tc)
        for name, args in [
            ("goto_altaz", {"alt": 45, "az": 125}),
            ("goto_radec_j2000", {"ra_h": 3.0, "dec_deg": 10.0}),
            ("tracking", {"on": True}),
            ("homed_override", {}),
        ]:
            ack = w.send(name, args, cid=name)
            assert ack["ok"] is False and ack["reason"] == STALLED, (name, ack)
        # tracking off and STOP stay available
        assert w.send("stop", cid="s")["ok"]
        assert w.send("tracking", {"on": False}, cid="t")["ok"]
        time.sleep(3.0)  # still stopped: no second stall stop for a mount that is not moving
        assert app.state.monitor.stalls == 1
        w.close()
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "stall_stop"]
    assert len(ev) == 1
    assert ev[0]["axis"] == 0 and ev[0]["dist_arcsec"] == 120000.0 and ev[0]["delivered"] is True
    assert ev[0]["duration_s"] >= 2.5


def test_monitor_alt_axis_stall(tmp_path):
    fake = stall_fake(1)
    with running(fake) as (tc, _, app):
        begin_stall(fake)
        wait_for(lambda: any(p == "/mount/stop" for p, _ in fake.writes), timeout=8)
        wait_for(lambda: homed_of(app) and homed_of(app)["reason"] == "stalled")
        wait_for(lambda: app.state.poller.latest.state == "STOPPED")
        assert app.state.poller.latest.state_reason == WARNING.format(n=1)
    assert [e["axis"] for e in audit_lines(tmp_path) if e["event"] == "stall_stop"] == [1]


def test_home_clears_the_stalled_guard(tmp_path):
    fake = stall_fake(0)
    with running(fake) as (tc, _, app):
        begin_stall(fake)
        wait_for(lambda: homed_of(app) and homed_of(app)["reason"] == "stalled", timeout=8)
        wait_for(lambda: app.state.poller.latest.state == "STOPPED")
        home_fast(fake, app)
        w = Win(tc)
        assert w.send("home")["ok"]
        wait_seq(app)
        wait_for(lambda: homed_state(app) == "homed")
        assert homed_of(app)["reason"] is None
        wait_for(lambda: app.state.poller.latest.state == "STOPPED")
        assert app.state.poller.latest.state_reason is None
        w.close()
    assert [e["how"] for e in events(tmp_path, "homed")] == ["no_motion"]


def test_disconnect_connect_starts_a_normal_session(tmp_path):
    fake = stall_fake(0)
    with running(fake) as (tc, _, app):
        begin_stall(fake)
        wait_for(lambda: homed_of(app) and homed_of(app)["reason"] == "stalled", timeout=8)
        wait_for(lambda: app.state.poller.latest.state == "STOPPED")
        home_fast(fake, app)
        w = Win(tc)
        assert w.send("disconnect", {"skip_home": True})["ok"]
        wait_seq(app)
        wait_for(lambda: homed_of(app) is None)
        fake.set(mount__axis0__dist_to_target_arcsec=0.0, mount__is_slewing=False)
        app.state.control.homed.begin("connect_sequence")  # what the CONNECT sequence does
        info = app.state.control.homed.info()
        assert info.state == "not_homed" and info.reason is None
        w.close()


def test_monitor_does_not_fire_when_alt_settles_first_and_az_keeps_moving():
    fake = FakePwi4()
    stop = threading.Event()

    def drive():
        t0 = time.monotonic()
        while not stop.is_set():
            az = 100.0 + 2.0 * (time.monotonic() - t0)
            fake.set(
                mount__is_slewing=True,
                mount__azimuth_degs=round(az, 4),
                mount__axis0__position_degs=round(az, 4),
                mount__axis0__dist_to_target_arcsec=150000.0,
                mount__axis1__dist_to_target_arcsec=0.0,
            )
            time.sleep(0.02)

    with running(fake) as (tc, _, app):
        th = threading.Thread(target=drive)
        th.start()
        time.sleep(5.0)
        stop.set()
        th.join(2)
        assert app.state.monitor.stalls == 0
        assert not any(p == "/mount/stop" for p, _ in fake.writes)


def test_monitor_does_not_fire_on_still_tracking_mount():
    fake = FakePwi4()
    with running(fake) as (tc, _, app):
        fake.set(
            mount__is_tracking=True,
            mount__axis0__dist_to_target_arcsec=2.0,
            mount__axis1__dist_to_target_arcsec=-1.0,
        )
        time.sleep(4.0)  # positions constant for 4 s: a pole / meridian pose, not a stall
        assert app.state.monitor.stalls == 0
        assert not any(p == "/mount/stop" for p, _ in fake.writes)


def test_user_stop_resets_the_stall_window():
    """A STOP mid-slew leaves a large target distance and (for up to 1.65 s, P2) is_slewing: the
    window restarts at the STOP, so it cannot read as a stall."""
    fake = stall_fake(0)
    fake.stop_ends_tracking = True
    with running(fake) as (tc, _, app):
        begin_stall(fake)
        time.sleep(1.8)
        w = Win(tc)
        assert w.send("stop")["ok"]
        fake.set(mount__is_slewing=True)  # PWI4 still reports slewing after the stop (P2)
        time.sleep(1.5)
        fake.set(mount__is_slewing=False)
        time.sleep(1.5)
        assert app.state.monitor.stalls == 0
        w.close()


# ------------------------------------------------------------------ PWI4's unwinding turn (P19)


def _turn(fake: FakePwi4, az0: float, rate: float, stop_evt: threading.Event, dur: float = 6.0):
    """PWI4's own unwinding turn after tracking_on: axis 0 sweeps at `rate` deg/s, slewing."""
    t0 = time.monotonic()
    while not stop_evt.is_set() and time.monotonic() - t0 < dur:
        if any(p == "/mount/stop" for p, _ in fake.writes):
            return
        az = (az0 + rate * (time.monotonic() - t0)) % 360.0
        fake.set(
            mount__is_slewing=True,
            mount__is_tracking=True,
            mount__azimuth_degs=round(az, 4),
            mount__axis0__position_degs=round(az, 4),
            mount__axis0__dist_to_target_arcsec=300000.0,
        )
        time.sleep(0.02)


def test_unwinding_turn_clear_of_the_sun_is_not_stopped(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    evt = threading.Event()
    with running(fake) as (tc, _, app):
        th = threading.Thread(target=_turn, args=(fake, 100.0, 20.0, evt, 5.0))
        th.start()
        th.join(7)
        assert app.state.monitor.stops == 0 and app.state.monitor.stalls == 0
        assert not any(p == "/mount/stop" for p, _ in fake.writes)


def test_unwinding_turn_towards_the_sun_is_stopped(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,250")  # on the turn's way, same altitude
    fake = FakePwi4()
    evt = threading.Event()
    with running(fake) as (tc, _, app):
        th = threading.Thread(target=_turn, args=(fake, 100.0, 20.0, evt, 8.0))
        th.start()
        wait_for(lambda: any(p == "/mount/stop" for p, _ in fake.writes), timeout=8)
        evt.set()
        th.join(3)
        assert app.state.monitor.stops == 1 and app.state.monitor.stalls == 0
        az = float(fake.text.split("mount.azimuth_degs=")[1].split("\n")[0])
        assert 250.0 - az > 15.0, az  # stopped before the disc


# ------------------------------------------------------------------ safety frame (P18)


def test_lim_el_and_sun_use_the_refracted_frame(monkeypatch):
    """A RA/Dec target whose refraction-free altitude is just below Lim EL (15) but whose observed
    (refracted, = what the mount will report) altitude is just above it is accepted; just below
    the observed altitude it is refused. The Sun disc is measured on the refracted Sun."""
    from datetime import UTC, datetime

    site = (14.3479, 100.5656, 12.0)
    with running() as (tc, fake, app):
        ephem = app.state.ephem
        now = datetime.now(UTC)
        w = Win(tc)
        # observed altitude 15.03 (unrefracted ~14.97): inside the limit in the mount's frame
        ra, dec = ephem.altaz_radec_j2000(site, 15.03, 250.0, now)
        un = ephem.radec_j2000_altaz(site, ra, dec, now, unrefracted=True)[0]
        assert un < 15.0 < 15.03
        ack = w.send("goto_radec_j2000", {"ra_h": ra, "dec_deg": dec}, "g1")
        assert ack["ok"] is True, ack
        ra, dec = ephem.altaz_radec_j2000(site, 14.97, 250.0, now)
        ack = w.send("goto_radec_j2000", {"ra_h": ra, "dec_deg": dec}, "g2")
        assert ack["ok"] is False and ack["reason"].startswith("Below station limit"), ack
        w.close()
    # the snapshot's Sun is the observed one: higher than the geometric altitude near the horizon
    geo = app.state.ephem.sun_altaz(site, unrefracted=True)
    obs = app.state.ephem.sun_altaz(site)
    assert abs(obs[1] - geo[1]) < 1e-3 and obs[0] >= geo[0]
