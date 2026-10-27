"""T3.4c: JOG and STEP commands against FakePwi4 (recorded requests; no real PWI4).

Covers the protocol, the keep-alive dead-man, release paths per origin, switch, step
serialization, the wrap-window refusal, the shared refusals, limit refusals, the snapshot fields,
the audit, STOP / link loss, the dry-run check and the monitor's look-ahead. The simulator checks
(rate accuracy, release latency, step accuracy, Lim EL margin) are in test_jog_pwi4sim.py.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from gateway.commands import CommandError, parse_command
from gateway.control import (
    R_JOG_SLEWING,
    R_JOGGING,
    R_NOT_HOMED,
    R_STEP,
)
from gateway.jog import (
    KEEPALIVE_TIMEOUT_S,
    JogController,
    r_axis_limit,
    r_step_limit,
)
from gateway.safety import (
    R_SUN,
    StallWatch,
    jog_margin_deg,
    lookahead_s,
    r_below,
)

from .conftest import FakePwi4
from .test_check import H, check
from .test_control import Win, audit_lines, cmd, running, wait_for

J0 = {"axis": 0, "dir": 1, "speed": 2.0}


def snap(tc) -> dict:
    return tc.get("/api/mount/status").json()


def paths(fake: FakePwi4) -> list[str]:
    return [p for p, _ in fake.writes]


def offsets(fake: FakePwi4) -> list[dict[str, str]]:
    return [q for p, q in fake.writes if p == "/mount/offset"]


def hold(w: Win, args: dict, seconds: float, cid: str = "k") -> None:
    """Keep-alive like the web client: the same command every 100 ms."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        assert w.send("jog", args, cid)["ok"]
        time.sleep(0.1)


@pytest.fixture(autouse=True)
def _no_sun(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")


# ------------------------------------------------------------------ protocol


@pytest.mark.parametrize(
    "name,args",
    [
        ("jog", {"axis": 0, "dir": 1, "speed": 3.0}),
        ("jog", {"axis": 2, "dir": 1, "speed": 1.0}),
        ("jog", {"axis": 0, "dir": 0, "speed": 1.0}),
        ("jog", {"axis": 0, "dir": 2, "speed": 1.0}),
        ("jog", {"axis": 0, "dir": 1}),
        ("jog", {"axis": 0, "dir": 1, "speed": 1.0, "x": 1}),
        ("jog", {"axis": True, "dir": 1, "speed": 1.0}),
        ("jog_stop", {"axis": 0}),
        ("step", {"axis": 0, "dir": 1, "size_arcmin": 7}),
        ("step", {"axis": 1, "dir": -1, "size_arcmin": 10.5}),
        ("step", {"axis": 1, "dir": 1}),
    ],
)
def test_bad_jog_step_args_are_refused(name, args):
    with pytest.raises(CommandError):
        parse_command(cmd(name, args))


def test_good_args_and_int_speed():
    c = parse_command(cmd("jog", {"axis": 1, "dir": -1, "speed": 5}))
    assert c.args == {"axis": 1, "dir": -1, "speed": 5.0}
    for s in (0.5, 1.0, 2.0, 5.0, 10.0):
        parse_command(cmd("jog", {"axis": 0, "dir": 1, "speed": s}))
    for n in (1, 5, 10, 30, 60):
        parse_command(cmd("step", {"axis": 0, "dir": 1, "size_arcmin": n}))
    parse_command(cmd("jog_stop"))


# ------------------------------------------------------------------ jog from STOPPED


def test_jog_from_stopped_tracking_on_rate_keepalive_silent_then_release(tmp_path):
    with running() as (tc, fake, app):
        assert snap(tc)["jog"] is None and snap(tc)["step"] is None
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        assert paths(fake) == ["/mount/tracking_on", "/mount/offset"]
        assert float(offsets(fake)[0]["axis0_set_rate_arcsec_per_sec"]) == 7200.0
        wait_for(lambda: snap(tc)["jog"] is not None)
        assert snap(tc)["jog"] == {"axis": 0, "dir": 1, "speed": 2.0, "origin": "stopped"}
        n = len(fake.writes)
        hold(w, J0, 0.8)  # keep-alives re-issue no PWI4 call
        assert len(fake.writes) == n
        assert w.send("jog_stop", None, "r")["ok"]
        assert paths(fake) == [
            "/mount/tracking_on",
            "/mount/offset",
            "/mount/offset",  # axis0_stop_rate
            "/mount/stop",
            "/mount/tracking_off",
            "/mount/offset",  # reset both
        ]
        assert offsets(fake)[1] == {"axis0_stop_rate": "1"}
        assert offsets(fake)[2] == {"axis0_reset": "1", "axis1_reset": "1"}
        assert "mount.is_tracking=false" in fake.text
        wait_for(lambda: snap(tc)["jog"] is None)
        w.close()
    ev = audit_lines(tmp_path)
    start = [e for e in ev if e["event"] == "jog_start"]
    stop = [e for e in ev if e["event"] == "jog_stop"]
    assert len(start) == 1 and start[0]["origin"] == "stopped" and start[0]["speed"] == 2.0
    assert start[0]["switch"] is False
    assert len(stop) == 1 and stop[0]["reason"] == "release" and stop[0]["origin"] == "stopped"
    # keep-alives are not audited as commands
    assert len([e for e in ev if e["event"] == "cmd" and e["cmd"] == "jog"]) == 1


def test_keepalive_timeout_stops_a_stopped_origin_jog(tmp_path):
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        t0 = time.monotonic()
        wait_for(lambda: "/mount/tracking_off" in paths(fake), timeout=2.0)
        dt = time.monotonic() - t0
        assert KEEPALIVE_TIMEOUT_S <= dt <= KEEPALIVE_TIMEOUT_S + 0.25, dt
        wait_for(lambda: snap(tc)["jog"] is None)
        assert "mount.is_tracking=false" in fake.text
        w.close()
    stop = [e for e in audit_lines(tmp_path) if e["event"] == "jog_stop"]
    assert [e["reason"] for e in stop] == ["keepalive_timeout"]


def test_keepalives_hold_the_jog_past_the_timeout():
    with running() as (tc, fake, _):
        w = Win(tc)
        hold(w, J0, 1.0)
        assert "/mount/stop" not in paths(fake)
        assert snap(tc)["jog"] is not None
        w.send("jog_stop", None, "r")
        w.close()


# ------------------------------------------------------------------ jog while TRACKING


def to_tracking(tc, fake: FakePwi4) -> None:
    """The (start-up stopped) fake mount starts tracking; the recorded calls restart empty."""
    fake.set(mount__is_tracking=True)
    wait_for(lambda: snap(tc)["state"] == "TRACKING")
    fake.calls.clear()


def test_jog_from_tracking_is_a_rate_offset_and_release_ends_stopped(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("jog", {"axis": 1, "dir": -1, "speed": 5.0}, "j1")["ok"]
        assert paths(fake) == ["/mount/offset"]  # no tracking_on
        assert float(offsets(fake)[0]["axis1_set_rate_arcsec_per_sec"]) == -18000.0
        wait_for(lambda: snap(tc)["jog"] is not None)
        assert snap(tc)["jog"]["origin"] == "tracking"
        assert w.send("jog_stop", None, "r")["ok"]
        assert paths(fake) == [
            "/mount/offset",
            "/mount/offset",  # axis1_stop_rate
            "/mount/stop",
            "/mount/tracking_off",
            "/mount/offset",  # reset both
        ]
        assert offsets(fake)[1] == {"axis1_stop_rate": "1"}
        assert offsets(fake)[2] == {"axis0_reset": "1", "axis1_reset": "1"}
        assert "mount.is_tracking=false" in fake.text
        wait_for(lambda: snap(tc)["jog"] is None)
        wait_for(lambda: snap(tc)["state"] == "STOPPED")
        w.close()
    stop = [e for e in audit_lines(tmp_path) if e["event"] == "jog_stop"]
    assert stop[0]["reason"] == "release" and stop[0]["origin"] == "tracking"


def test_keepalive_timeout_from_tracking_ends_stopped(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        wait_for(lambda: "/mount/tracking_off" in paths(fake), timeout=2.0)
        wait_for(lambda: {"axis0_reset": "1", "axis1_reset": "1"} in offsets(fake))
        assert "/mount/stop" in paths(fake) and "/mount/tracking_on" not in paths(fake)
        assert offsets(fake)[1] == {"axis0_stop_rate": "1"}
        assert "mount.is_tracking=false" in fake.text
        w.close()
    stop = [e for e in audit_lines(tmp_path) if e["event"] == "jog_stop"]
    assert [(e["reason"], e["origin"]) for e in stop] == [("keepalive_timeout", "tracking")]


def test_disconnect_while_jogging_from_tracking_ends_stopped(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        w.close()
        wait_for(lambda: {"axis0_reset": "1", "axis1_reset": "1"} in offsets(fake), timeout=3.0)
        assert "/mount/stop" in paths(fake) and "/mount/tracking_off" in paths(fake)
        wait_for(lambda: snap(tc)["jog"] is None)
    stop = [e for e in audit_lines(tmp_path) if e["event"] == "jog_stop"]
    assert stop and stop[0]["origin"] == "tracking"


# ------------------------------------------------------------------ switch


def test_switch_speed_and_axis_while_jogging(tmp_path):
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        faster = {"axis": 0, "dir": 1, "speed": 5.0}
        assert w.send("jog", faster, "j2")["ok"]
        assert paths(fake) == ["/mount/tracking_on", "/mount/offset", "/mount/offset"]
        assert offsets(fake)[1] == {"axis0_set_rate_arcsec_per_sec": "18000.0"}
        other = {"axis": 1, "dir": -1, "speed": 1.0}
        assert w.send("jog", other, "j3")["ok"]
        assert offsets(fake)[2] == {
            "axis1_set_rate_arcsec_per_sec": "-3600.0",
            "axis0_stop_rate": "1",
        }
        assert "/mount/stop" not in paths(fake) and paths(fake).count("/mount/tracking_on") == 1
        wait_for(lambda: snap(tc)["jog"] == {**other, "origin": "stopped"})
        # reversing direction on the same axis is a switch too
        assert w.send("jog", {"axis": 1, "dir": 1, "speed": 1.0}, "j4")["ok"]
        assert offsets(fake)[3] == {"axis1_set_rate_arcsec_per_sec": "3600.0"}
        w.send("jog_stop", None, "r")
        w.close()
    start = [e for e in audit_lines(tmp_path) if e["event"] == "jog_start"]
    assert [e["switch"] for e in start] == [False, True, True, True]


# ------------------------------------------------------------------ wrap window (P19)


def test_jog_from_stopped_outside_the_wrap_window_recentres_it_first():
    """T3.4e option B: no refusal; the window is re-centred on the axis, then tracking_on (P19:
    PWI4 would otherwise turn the mount to normalise the axis)."""
    fake = FakePwi4()
    fake.set(mount__axis0__position_degs=250.0)  # wrap window is [-180, 180]
    with running(fake) as (tc, _, audit_dir):
        w = Win(tc)
        ack = w.send("jog", J0, "j1")
        assert ack["ok"], ack
        p = paths(fake)
        assert p.index("/mount/set_axis0_wrap_range_min") < p.index("/mount/tracking_on")
        q = [q for pp, q in fake.writes if pp == "/mount/set_axis0_wrap_range_min"][0]
        assert abs(float(q["degs"]) - 70.0) < 1e-6  # 250 - 180
        w.send("jog_stop", None, "r")
        w.close()


# ------------------------------------------------------------------ refusals


def test_shared_refusals_not_homed_axes_disabled_other_window():
    with running(homed=False) as (tc, fake, _):
        w = Win(tc)
        for name, args in (("jog", J0), ("step", {"axis": 0, "dir": 1, "size_arcmin": 5})):
            ack = w.send(name, args, "x")
            assert not ack["ok"] and ack["reason"] == R_NOT_HOMED
        assert paths(fake) == []
        w.close()
    with running() as (tc, fake, _):
        fake.set(mount__axis1__is_enabled=False)
        wait_for(lambda: snap(tc)["state"] == "FAULT")
        w = Win(tc)
        assert w.send("jog", J0, "x")["reason"] == "Axis 1 disabled"
        assert w.send("step", {"axis": 0, "dir": 1, "size_arcmin": 5}, "y")["reason"] == (
            "Axis 1 disabled"
        )
        assert paths(fake) == []
        w.close()
    with running() as (tc, fake, _):
        a, b = Win(tc), Win(tc)
        assert a.send("jog", J0, "j1")["ok"]
        ack = b.send("jog", J0, "j2")
        assert not ack["ok"] and ack["reason"] == "Another window has control"
        assert b.send("jog_stop", None, "r2")["reason"] == "Another window has control"
        assert a.send("jog", J0, "k")["ok"]
        wait_for(lambda: snap(tc)["jog"] is not None)
        a.send("jog_stop", None, "r")
        a.close()
        b.close()


def test_slewing_refuses_jog_and_step_but_not_a_running_jog():
    c = JogController(
        SimpleNamespace(poller=SimpleNamespace(latest=SimpleNamespace(state="SLEWING")))
    )
    st = SimpleNamespace(
        mount=SimpleNamespace(
            is_tracking=False,
            axis0_wrap_range_min_degs=-180.0,
            axis0=SimpleNamespace(
                position_degs=10.0, min_mech_position_degs=-300.0, max_mech_position_degs=300.0
            ),
            axis1=SimpleNamespace(
                position_degs=40.0, min_mech_position_degs=-40.0, max_mech_position_degs=91.0
            ),
        )
    )
    jog = parse_command(cmd("jog", J0))
    step = parse_command(cmd("step", {"axis": 0, "dir": 1, "size_arcmin": 5}))
    assert c.refusal(jog, st) == R_JOG_SLEWING == "Slewing; press STOP first"
    assert c.refusal(step, st) == R_JOG_SLEWING
    st.mount.is_tracking = True
    assert c.refusal(jog, st) == R_JOG_SLEWING
    c.svc.poller.latest.state = "STOPPED"
    assert c.refusal(jog, st) is None and c.refusal(step, st) is None


def test_goto_and_jog_while_jogging_or_stepping_are_refused():
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        assert w.send("goto_altaz", {"alt": 50.0, "az": 100.0}, "g")["reason"] == R_JOGGING
        assert w.send("tracking", {"on": False}, "t")["reason"] == R_JOGGING
        s = w.send("step", {"axis": 0, "dir": 1, "size_arcmin": 5}, "s")
        assert not s["ok"] and s["reason"] == R_JOGGING
        w.send("jog_stop", None, "r")
        wait_for(lambda: snap(tc)["jog"] is None)
        assert w.send("step", {"axis": 0, "dir": 1, "size_arcmin": 30}, "s2")["ok"]
        assert w.send("goto_altaz", {"alt": 50.0, "az": 100.0}, "g2")["reason"] == R_STEP
        w.close()


def test_jog_stop_when_idle_is_a_noop_ack():
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("jog_stop", None, "r")["ok"]
        assert paths(fake) == []
        w.close()


# ------------------------------------------------------------------ limits (G1-G3)


def test_jog_toward_lim_el_refused_up_front_scaled_by_speed():
    fake = FakePwi4()
    fake.set(mount__altitude_degs=18.0, mount__axis1__position_degs=18.0)  # Lim EL 15
    with running(fake) as (tc, _, app):
        lim = app.state.safety.lim_el()
        assert lim == 15.0
        w = Win(tc)
        # 10 deg/s needs ~7 deg to stop: 3 deg above the limit is too close; 0.5 deg/s is fine
        ack = w.send("jog", {"axis": 1, "dir": -1, "speed": 10.0}, "a")
        assert not ack["ok"] and ack["reason"] == r_below(lim) and ack["code"] == "below_lim_el"
        assert paths(fake) == []
        assert w.send("jog", {"axis": 1, "dir": -1, "speed": 0.5}, "b")["ok"]
        w.send("jog_stop", None, "r")
        # moving up (away from the limit) is always allowed, also from below it
        fake.set(mount__altitude_degs=10.0, mount__axis1__position_degs=10.0)
        time.sleep(0.4)
        assert not w.send("jog", {"axis": 1, "dir": -1, "speed": 0.5}, "c")["ok"]
        assert w.send("jog", {"axis": 1, "dir": 1, "speed": 10.0}, "d")["ok"]
        w.send("jog_stop", None, "r2")
        w.close()


def test_jog_toward_the_sun_refused_away_allowed(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,128")  # fake mount alt 45 / az 120: 5.7 deg apart
    with running() as (tc, fake, _):
        w = Win(tc)
        ack = w.send("jog", {"axis": 0, "dir": 1, "speed": 1.0}, "a")
        assert not ack["ok"] and ack["reason"] == R_SUN
        assert paths(fake) == []
        assert w.send("jog", {"axis": 0, "dir": -1, "speed": 1.0}, "b")["ok"]  # recovery
        w.send("jog_stop", None, "r")
        assert not w.send("step", {"axis": 0, "dir": 1, "size_arcmin": 30}, "c")["ok"]
        w.close()


def test_jog_and_step_into_a_mechanical_limit_are_refused():
    fake = FakePwi4()
    fake.set(mount__axis1__position_degs=91.0)  # the +91 deg limit
    with running(fake) as (tc, _, _):
        w = Win(tc)
        ack = w.send("jog", {"axis": 1, "dir": 1, "speed": 1.0}, "a")
        assert not ack["ok"] and ack["reason"] == r_axis_limit(1)
        assert w.send("jog", {"axis": 1, "dir": -1, "speed": 1.0}, "b")["ok"]  # away from it
        w.send("jog_stop", None, "r")
        wait_for(lambda: snap(tc)["jog"] is None)
        assert w.send("step", {"axis": 1, "dir": 1, "size_arcmin": 1}, "c")["reason"] == (
            r_step_limit(1)
        )
        w.close()


def test_step_down_below_lim_el_is_refused():
    fake = FakePwi4()
    fake.set(mount__altitude_degs=15.1, mount__axis1__position_degs=15.1)
    with running(fake) as (tc, _, app):
        to_tracking(tc, fake)
        lim = app.state.safety.lim_el()
        w = Win(tc)
        ack = w.send("step", {"axis": 1, "dir": -1, "size_arcmin": 10}, "a")
        assert not ack["ok"] and ack["reason"] == r_below(lim)
        assert w.send("step", {"axis": 1, "dir": 1, "size_arcmin": 10}, "b")["ok"]
        w.close()


# ------------------------------------------------------------------ step


def test_step_while_tracking_add_arcsec_serialized_and_done(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        a = {"axis": 1, "dir": 1, "size_arcmin": 10}
        assert w.send("step", a, "s1")["ok"]
        assert paths(fake) == ["/mount/offset"]
        assert offsets(fake)[0] == {"axis1_add_arcsec": "600.0"}
        wait_for(lambda: snap(tc)["step"] is not None)
        assert snap(tc)["step"] == {"axis": 1, "dir": 1, "size_arcmin": 10}
        second = w.send("step", {"axis": 0, "dir": -1, "size_arcmin": 5}, "s2")
        assert not second["ok"] and second["reason"] == R_STEP == "Step in progress"
        assert w.send("jog", J0, "j")["reason"] == R_STEP
        assert len(offsets(fake)) == 1
        wait_for(lambda: snap(tc)["step"] is None, timeout=3.0)  # dist_to_target is 0 in the fake
        assert w.send("step", {"axis": 0, "dir": -1, "size_arcmin": 60}, "s3")["ok"]
        assert offsets(fake)[1] == {"axis0_add_arcsec": "-3600.0"}
        w.close()
    ev = audit_lines(tmp_path)
    steps = [e for e in ev if e["event"] == "step"]
    assert [(e["axis"], e["dir"], e["size_arcmin"], e["origin"]) for e in steps][:2] == [
        (1, 1, 10, "tracking"),
        (0, -1, 60, "tracking"),
    ]
    assert ev and [e for e in ev if e["event"] == "step_end"][0]["how"] == "done"


def test_step_from_tracking_resumes_tracking_if_pwi4_stopped_it(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("step", {"axis": 1, "dir": 1, "size_arcmin": 10}, "s1")["ok"]
        fake.set(mount__is_tracking=False)  # PWI4 dropped tracking during the step
        wait_for(lambda: any(e["event"] == "step_end" for e in audit_lines(tmp_path)), timeout=3.0)
        assert paths(fake).count("/mount/tracking_on") == 1
        assert {"axis0_reset": "1", "axis1_reset": "1"} not in offsets(fake)  # offset stays
        w.close()
    end = [e for e in audit_lines(tmp_path) if e["event"] == "step_end"]
    assert end[0]["tracking_resumed"] is True and "note" not in end[0]


def test_step_from_tracking_that_is_still_tracking_sends_nothing_more():
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("step", {"axis": 1, "dir": 1, "size_arcmin": 10}, "s1")["ok"]
        wait_for(lambda: snap(tc)["step"] is None, timeout=3.0)
        assert paths(fake) == ["/mount/offset"]
        w.close()


def test_step_from_tracking_outside_wrap_window_recentres_and_resumes(tmp_path):
    """T3.4e option B: the window is re-centred on the axis, then tracking_on (no PWI4 turn)."""
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("step", {"axis": 1, "dir": 1, "size_arcmin": 10}, "s1")["ok"]
        fake.set(mount__is_tracking=False, mount__axis0__position_degs=250.0)  # window [-180,180]
        wait_for(lambda: any(e["event"] == "step_end" for e in audit_lines(tmp_path)), timeout=3.0)
        p = paths(fake)
        last_on = len(p) - 1 - p[::-1].index("/mount/tracking_on")
        assert p.index("/mount/set_axis0_wrap_range_min") < last_on
        w.close()
    end = [e for e in audit_lines(tmp_path) if e["event"] == "step_end"]
    assert end[0]["tracking_resumed"] is True


def test_step_from_stopped_is_one_validated_goto_without_tracking_on(tmp_path):
    with running() as (tc, fake, _):
        w = Win(tc)
        a = {"axis": 1, "dir": 1, "size_arcmin": 10}
        assert w.send("step", a, "s1")["ok"]
        ps = paths(fake)
        assert ps[0] == "/mount/tracking_off" and ps[-1] == "/mount/goto_alt_az"
        assert "/mount/tracking_on" not in ps and "/mount/offset" not in ps
        goto = [q for p, q in fake.writes if p == "/mount/goto_alt_az"]
        assert len(goto) == 1
        assert float(goto[0]["alt_degs"]) == pytest.approx(45.0 + 10 / 60)
        assert float(goto[0]["az_degs"]) == pytest.approx(120.0)
        second = w.send("step", a, "s2")
        assert not second["ok"] and second["reason"] == R_STEP
        assert paths(fake).count("/mount/goto_alt_az") == 1
        wait_for(lambda: snap(tc)["step"] is not None)
        time.sleep(0.5)
        assert snap(tc)["step"] is not None  # PWI4 still reports slewing: not done
        fake.set(mount__is_slewing=False)  # ... arrived
        wait_for(lambda: snap(tc)["step"] is None, timeout=3.0)
        # axis 0 -> azimuth
        ack = w.send("step", {"axis": 0, "dir": -1, "size_arcmin": 60}, "s3")
        assert ack["ok"], ack["reason"]
        goto = [q for p, q in fake.writes if p == "/mount/goto_alt_az"][-1]
        assert float(goto["az_degs"]) == pytest.approx(119.0)
        w.close()
    steps = [e for e in audit_lines(tmp_path) if e["event"] == "step"]
    assert steps[0]["origin"] == "stopped"


def test_stop_cancels_a_step_and_clears_offsets(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("step", {"axis": 1, "dir": 1, "size_arcmin": 60}, "s1")["ok"]
        assert w.send("stop", None, "st")["ok"]
        wait_for(lambda: snap(tc)["step"] is None)
        wait_for(lambda: {"axis0_reset": "1", "axis1_reset": "1"} in offsets(fake))
        w.close()
    end = [e for e in audit_lines(tmp_path) if e["event"] == "step_end"]
    assert end and end[0]["how"] == "stop"


# ------------------------------------------------------------------ STOP / link loss / audit


def test_stop_during_jog_ends_it_and_cleans_the_rate(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        to_tracking(tc, fake)
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        assert w.send("stop", None, "st")["ok"]
        wait_for(lambda: snap(tc)["jog"] is None)
        # after the stop: both rates zero, tracking off, offsets reset (tracking-origin too)
        wait_for(lambda: {"axis0_reset": "1", "axis1_reset": "1"} in offsets(fake))
        ps = paths(fake)
        assert ps.index("/mount/stop") < ps.index("/mount/tracking_off")
        assert {"axis0_stop_rate": "1", "axis1_stop_rate": "1"} in offsets(fake)
        n = len(fake.writes)
        time.sleep(0.5)  # the dead man does not fire a second release
        assert len(fake.writes) == n
        w.close()
    stop = [e for e in audit_lines(tmp_path) if e["event"] == "jog_stop"]
    assert [e["reason"] for e in stop] == ["stop"]


def test_link_loss_ends_the_jog(tmp_path):
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        w.close()
        wait_for(lambda: "/mount/stop" in paths(fake), timeout=3.0)
        wait_for(lambda: snap(tc)["jog"] is None)
        wait_for(lambda: {"axis0_reset": "1", "axis1_reset": "1"} in offsets(fake))
    ev = audit_lines(tmp_path)
    stop = [e for e in ev if e["event"] == "jog_stop"]
    assert [e["reason"] for e in stop] == ["link_loss"]
    assert any(e["event"] == "link_loss_stop" for e in ev)


def test_release_control_ends_the_jog():
    with running() as (tc, fake, _):
        w = Win(tc)
        assert w.send("jog", J0, "j1")["ok"]
        assert w.send("release_control", None, "rc")["ok"]
        assert snap(tc)["jog"] is None and "/mount/stop" in paths(fake)
        w.close()


def drive_alt(fake: FakePwi4, alt0: float, rate: float, stop_evt: threading.Event, log) -> None:
    t0 = time.monotonic()
    while not stop_evt.is_set():
        if any(p == "/mount/stop" for p, _ in fake.writes):
            return
        alt = alt0 + rate * (time.monotonic() - t0)
        fake.set(
            mount__altitude_degs=round(alt, 4),
            mount__axis1__position_degs=round(alt, 4),
            mount__is_slewing=True,
        )
        log.append(alt)
        time.sleep(0.02)


def test_jog_at_10_deg_s_toward_lim_el_is_stopped_early_enough(tmp_path):
    fake = FakePwi4()
    fake.set(mount__altitude_degs=60.0, mount__axis1__position_degs=60.0)
    evt, log = threading.Event(), []
    args = {"axis": 1, "dir": -1, "speed": 10.0}
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("jog", args, "j1")["ok"]
        th = threading.Thread(target=drive_alt, args=(fake, 60.0, -10.0, evt, log))
        th.start()
        end = time.monotonic() + 8.0
        while time.monotonic() < end and "/mount/stop" not in paths(fake):
            w.send("jog", args, "k")
            time.sleep(0.1)
        evt.set()
        th.join(2)
        assert "/mount/stop" in paths(fake)
        # the real mount would still coast ~ v^2 / (2 a) = 2.5 deg (reported 20 deg/s^2) + the
        # latency of one more monitor cycle; the stop must have been requested early enough
        coast = 10.0**2 / (2 * 20.0) + 10.0 * 0.25
        assert log[-1] - coast >= 15.0, (log[-1], coast)
        wait_for(lambda: snap(tc)["jog"] is None)
        w.close()
    stop = [e for e in audit_lines(tmp_path) if e["event"] == "jog_stop"]
    assert [e["reason"] for e in stop] == ["limit"]


# ------------------------------------------------------------------ dry-run check, snapshot


def test_dry_check_matches_real_jog_and_step_verdicts():
    fake = FakePwi4()
    fake.set(mount__altitude_degs=18.0, mount__axis1__position_degs=18.0)
    with running(fake) as (tc, _, app):
        lim = app.state.safety.lim_el()
        bad = {"axis": 1, "dir": -1, "speed": 10.0}
        d = check(tc, "jog", bad)
        assert not d["ok"] and d["reason"] == r_below(lim) and d["code"] == "below_lim_el"
        d = check(tc, "jog", {"axis": 1, "dir": 1, "speed": 10.0})
        assert d["ok"] and d["reason"] is None
        d = check(tc, "step", {"axis": 0, "dir": 1, "size_arcmin": 60})
        assert d["ok"] and d["target"]["az"] == pytest.approx(121.0)
        assert d["target"]["alt"] == pytest.approx(18.0)
        d = check(tc, "step", {"axis": 1, "dir": -1, "size_arcmin": 60})
        assert d["ok"] and d["target"]["alt"] == pytest.approx(17.0)
        r = tc.post("/api/mount/check", json={"cmd": "jog", "args": {"axis": 0}}, headers=H)
        assert r.status_code == 422
        assert paths(fake) == []  # a dry run never writes


def test_snapshot_fields_null_when_disconnected():
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        body = snap(tc)
        assert body["jog"] is None and body["step"] is None
        fake.set(mount__is_connected=False)
        wait_for(lambda: snap(tc)["state"] == "DISCONNECTED")
        assert snap(tc)["jog"] is None


# ------------------------------------------------------------------ monitor look-ahead / stall


def test_lookahead_covers_the_stopping_distance_up_to_10_deg_s():
    assert lookahead_s(0.0, 20.0) == 0.5  # never below the old fixed look-ahead
    assert lookahead_s(1.0, 20.0) == pytest.approx(0.5)
    assert lookahead_s(10.0, 20.0) == pytest.approx(0.7)
    # distance needed (latency 0.45 s + v^2 / 2a) is covered exactly
    for v, a in ((10.0, 20.0), (10.0, 5.0), (5.0, 20.0)):
        assert v * lookahead_s(v, a) == pytest.approx(v * 0.45 + v * v / (2 * a), abs=1e-9)
        assert jog_margin_deg(v, a) == pytest.approx(v * lookahead_s(v, a))
    assert jog_margin_deg(10.0, 20.0) == pytest.approx(7.0)


def _axis(pos, dist=0.0):
    return SimpleNamespace(
        position_degs=pos,
        dist_to_target_arcsec=dist,
        target_mech_position_degs=None,
        setpoint_velocity_degs_per_sec=None,
    )


def test_stall_watch_treats_a_jogged_axis_as_commanded():
    """A jog into a mechanical limit: the axis stops moving; its reported target distance may
    stay small, so the jogged axis counts as commanded."""
    mount = SimpleNamespace(axis0=_axis(300.0), axis1=_axis(45.0))
    w = StallWatch()
    t, ev = 0.0, None
    while t < 6.0 and ev is None:
        ev = w.update(t, mount, True, jog_axis=0)
        t += 0.1
    assert ev is not None and ev.axis == 0 and 2.4 <= t <= 2.8
    w2 = StallWatch()  # without a jog the same still mount is not a stall
    assert all(w2.update(i * 0.1, mount, True) is None for i in range(80))
    w3 = StallWatch()  # a moving jogged axis never trips
    for i in range(80):
        m = SimpleNamespace(axis0=_axis(100.0 + i * 0.2), axis1=_axis(45.0))
        assert w3.update(i * 0.1, m, True, jog_axis=0) is None
