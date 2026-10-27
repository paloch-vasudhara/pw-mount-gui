"""T3.4e: azimuth-jog wrap handling (option B) against FakePwi4: the wrap-window slide, the
mechanical-limit stop with its margin, the `jog_limit` prompt, the 360 deg `wrap_turn` and
`wrap_turn_cancel`, the snapshot fields, the audit and the dry-run check. The simulator checks are
in test_jog_pwi4sim.py.

The fake mount: axis 0 at 120 deg (mechanical range -300 .. +300), wrap window minimum -180,
20 deg/s^2, alt 45 / az 120, tracking and slewing flags only. The tests move it by hand.
"""

from __future__ import annotations

import threading
import time

import pytest

from gateway.commands import CommandError, parse_command
from gateway.control import (
    R_AZ_LIMIT,
    R_JOGGING,
    R_NO_ROOM,
    R_NOT_HOMED,
    R_OTHER_HAS_CONTROL,
    R_STALLED,
    R_TURN,
)
from gateway.jog import (
    LIMIT_EXTRA_DEG,
    WRAP_SLIDE_MARGIN_DEG,
)
from gateway.safety import R_PATH_SUN, R_SUN, jog_margin_deg, r_below, turn_duration_s

from .conftest import FakePwi4
from .test_check import H, check
from .test_control import Win, audit_lines, cmd, running, wait_for
from .test_jog import J0, hold, offsets, paths, snap

WRAP = "/mount/set_axis0_wrap_range_min"
AT_LIMIT = 291.5  # +300 mechanical limit minus the 10 deg/s margin (9.0) = 291.0


def wraps(fake: FakePwi4) -> list[float]:
    return [float(q["degs"]) for p, q in fake.writes if p == WRAP]


def at(fake: FakePwi4, x0: float, wmin: float = -180.0, **extra) -> None:
    """Axis 0 at x0 (azimuth = x0 mod 360), wrap window minimum wmin."""
    fake.set(
        mount__axis0__position_degs=x0,
        mount__azimuth_degs=x0 % 360.0,
        mount__axis0_wrap_range_min_degs=wmin,
        **extra,
    )


def fresh(fake: FakePwi4, **kw) -> FakePwi4:
    at(fake, kw.pop("x0", 120.0), kw.pop("wmin", -180.0), **kw)
    return fake


@pytest.fixture(autouse=True)
def _no_sun(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")


# ------------------------------------------------------------------ protocol


def test_protocol_wrap_commands_take_no_args():
    assert parse_command(cmd("wrap_turn")).args == {}
    assert parse_command(cmd("wrap_turn_cancel")).args == {}
    for name in ("wrap_turn", "wrap_turn_cancel"):
        with pytest.raises(CommandError):
            parse_command(cmd(name, {"dir": 1}))


# ------------------------------------------------------------------ wrap slide


def test_slide_does_not_trigger_far_from_the_edge_nor_for_axis_1_nor_when_moving_away(tmp_path):
    fake = fresh(FakePwi4())  # pos 120, window -180..180: 60 deg from the + edge
    with running(fake) as (tc, _, _):
        w = Win(tc)
        hold(w, J0, 0.8)
        assert wraps(fake) == []
        w.send("jog_stop", None, "r")
        at(fake, 170.0)  # 10 deg from the + edge, but an axis-1 jog does not care
        assert w.send("jog", {"axis": 1, "dir": 1, "speed": 1.0}, "a1")["ok"]
        hold(w, {"axis": 1, "dir": 1, "speed": 1.0}, 0.8, "a1")
        w.send("jog_stop", None, "r")
        # moving away from the near edge: 330 deg from the - edge
        assert w.send("jog", {"axis": 0, "dir": -1, "speed": 2.0}, "m")["ok"]
        hold(w, {"axis": 0, "dir": -1, "speed": 2.0}, 0.8, "m")
        w.send("jog_stop", None, "r")
        assert wraps(fake) == []
        w.close()
    assert not [e for e in audit_lines(tmp_path) if e["event"] == "wrap_slide"]


@pytest.mark.parametrize(
    "d,x0,wmin,edge_dist",
    [(1, 150.0, -180.0, 30.0), (-1, -150.0, -180.0, 30.0), (1, 141.0, -180.0, 39.0)],
)
def test_slide_recentres_the_window_within_the_margin(tmp_path, d, x0, wmin, edge_dist):
    assert edge_dist < WRAP_SLIDE_MARGIN_DEG == 40.0
    fake = fresh(FakePwi4(), x0=x0, wmin=wmin)
    args = {"axis": 0, "dir": d, "speed": 2.0}
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("jog", args, "j")["ok"]
        wait_for(lambda: wraps(fake) != [], timeout=2.0)
        hold(w, args, 0.2, "j")
        assert wraps(fake) == [x0 - 180.0]  # the only PWI4 write besides the jog's own
        assert [p for p in paths(fake) if p != WRAP] == ["/mount/tracking_on", "/mount/offset"]
        fake.set(mount__axis0_wrap_range_min_degs=x0 - 180.0)  # PWI4 took it
        hold(w, args, 0.8, "j")  # centred: no further slide
        assert len(wraps(fake)) == 1
        w.send("jog_stop", None, "r")
        w.close()
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "wrap_slide"]
    assert len(ev) == 1
    assert ev[0]["old_min"] == wmin and ev[0]["new_min"] == x0 - 180.0 and ev[0]["dir"] == d


def test_slide_waits_for_a_fresh_status_before_the_next_slide():
    fake = fresh(FakePwi4(), x0=150.0)
    args = {"axis": 0, "dir": 1, "speed": 2.0}
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("jog", args, "j")["ok"]
        hold(w, args, 1.3, "j")  # the status never changes: at most one re-send per 0.5 s
        assert 2 <= len(wraps(fake)) <= 3
        w.send("jog_stop", None, "r")
        w.close()


def test_failed_slide_ends_the_jog(tmp_path):
    fake = fresh(FakePwi4(), x0=150.0)
    fake.fail_writes.add(WRAP)
    args = {"axis": 0, "dir": 1, "speed": 2.0}
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("jog", args, "j")["ok"]
        wait_for(lambda: "/mount/stop" in paths(fake), timeout=2.0)
        wait_for(lambda: snap(tc)["jog"] is None)
        w.close()
    ev = audit_lines(tmp_path)
    assert [e["reason"] for e in ev if e["event"] == "jog_stop"] == ["wrap_slide_failed"]
    assert any(e["event"] == "wrap_slide_failed" for e in ev)


# ------------------------------------------------------------------ mechanical limit


def margin(speed: float) -> float:
    return jog_margin_deg(speed, 20.0) + LIMIT_EXTRA_DEG


def test_margin_is_the_stopping_distance_plus_2_deg():
    assert LIMIT_EXTRA_DEG == 2.0
    assert margin(10.0) == pytest.approx(9.0)  # 10 * 0.7 + 2
    assert margin(0.5) == pytest.approx(2.25)  # 0.5 * 0.5 + 2


def test_jog_stops_at_the_margin_and_sets_jog_limit(tmp_path):
    fake = fresh(FakePwi4(), x0=250.0, wmin=70.0)
    args = {"axis": 0, "dir": 1, "speed": 10.0}
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("jog", args, "j")["ok"]
        hold(w, args, 0.4, "j")
        assert snap(tc)["jog"] is not None and snap(tc)["jog_limit"] is None
        at(fake, 290.9, wmin=70.0)  # 0.1 deg before the stop point (300 - 9)
        hold(w, args, 0.4, "j")
        assert snap(tc)["jog"] is not None
        at(fake, 291.2, wmin=70.0)
        wait_for(lambda: snap(tc)["jog"] is None, timeout=2.0)
        assert "/mount/stop" in paths(fake) and "/mount/tracking_off" in paths(fake)
        s = snap(tc)["jog_limit"]
        assert s["axis"] == 0 and s["dir"] == 1 and s["limit_deg"] == 300.0
        assert s["turn_deg"] == 360.0 and s["est_s"] > 0
        assert snap(tc)["turn"] is None
        w.close()
    ev = audit_lines(tmp_path)
    assert [e["reason"] for e in ev if e["event"] == "jog_stop"] == ["mech_limit"]
    lim = [e for e in ev if e["event"] == "jog_limit"]
    assert len(lim) == 1 and lim[0]["how"] == "jog_end" and lim[0]["limit_deg"] == 300.0
    assert lim[0]["margin_deg"] == pytest.approx(9.0)
    assert not any(e["event"] == "stall_stop" for e in ev)


def test_negative_limit_and_slow_speed_margin():
    fake = fresh(FakePwi4(), x0=-298.0, wmin=-360.0)
    with running(fake) as (tc, _, _):
        w = Win(tc)
        a = {"axis": 0, "dir": -1, "speed": 0.5}
        ack = w.send("jog", a, "a")  # -298 is within 2.25 of -300
        assert not ack["ok"] and ack["reason"] == R_AZ_LIMIT and ack["code"] == "az_limit"
        wait_for(lambda: snap(tc)["jog_limit"] is not None)
        s = snap(tc)["jog_limit"]
        assert s["dir"] == -1 and s["limit_deg"] == -300.0
        at(fake, -297.5, wmin=-360.0)  # 2.5 deg from the limit: 0.5 deg/s is allowed
        time.sleep(0.4)
        assert w.send("jog", a, "b")["ok"]
        wait_for(lambda: snap(tc)["jog_limit"] is None)  # a started az jog drops the prompt
        w.send("jog_stop", None, "r")
        w.close()


def test_jog_at_the_margin_same_direction_refused_other_direction_clears(tmp_path):
    fake = fresh(FakePwi4(), x0=AT_LIMIT, wmin=AT_LIMIT - 180.0)
    with running(fake) as (tc, _, _):
        w = Win(tc)
        n = len(fake.writes)
        ack = w.send("jog", {"axis": 0, "dir": 1, "speed": 10.0}, "a")
        assert not ack["ok"] and ack["reason"] == R_AZ_LIMIT == (
            "Azimuth at its mechanical limit: turn 360° or jog the other way"
        )
        assert ack["code"] == "az_limit"
        assert len(fake.writes) == n  # nothing went to PWI4
        wait_for(lambda: snap(tc)["jog_limit"] is not None)
        # a held key re-sends: still refused, one audit entry for the limit
        for i in range(3):
            assert not w.send("jog", {"axis": 0, "dir": 1, "speed": 10.0}, f"b{i}")["ok"]
        # the other way clears the prompt
        assert w.send("jog", {"axis": 0, "dir": -1, "speed": 2.0}, "c")["ok"]
        wait_for(lambda: snap(tc)["jog_limit"] is None)
        w.send("jog_stop", None, "r")
        # an axis-1 jog does not touch / set it
        assert w.send("jog", {"axis": 1, "dir": 1, "speed": 1.0}, "d")["ok"]
        w.send("jog_stop", None, "r2")
        w.close()
    ev = audit_lines(tmp_path)
    lim = [e for e in ev if e["event"] == "jog_limit"]
    assert len(lim) == 1 and lim[0]["how"] == "refused"
    refused = [
        e for e in ev if e["event"] == "cmd" and e["cmd"] == "jog" and e["reason"] == R_AZ_LIMIT
    ]
    assert len(refused) == 1


def test_jog_limit_lapses_when_the_axis_leaves_the_margin():
    fake = fresh(FakePwi4(), x0=AT_LIMIT, wmin=AT_LIMIT - 180.0)
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert not w.send("jog", {"axis": 0, "dir": 1, "speed": 10.0}, "a")["ok"]
        wait_for(lambda: snap(tc)["jog_limit"] is not None)
        at(fake, 200.0, wmin=20.0)  # moved away by something else (a goto)
        wait_for(lambda: snap(tc)["jog_limit"] is None)
        w.close()


def test_dry_check_az_limit_has_no_side_effect():
    fake = fresh(FakePwi4(), x0=AT_LIMIT, wmin=AT_LIMIT - 180.0)
    with running(fake) as (tc, _, _):
        d = check(tc, "jog", {"axis": 0, "dir": 1, "speed": 10.0})
        assert not d["ok"] and d["reason"] == R_AZ_LIMIT and d["code"] == "az_limit"
        d = check(tc, "jog", {"axis": 0, "dir": -1, "speed": 10.0})
        assert d["ok"]
        time.sleep(0.3)
        assert snap(tc)["jog_limit"] is None
        assert paths(fake) == []


# ------------------------------------------------------------------ wrap turn


def limit_state(tc, fake: FakePwi4, x0: float = AT_LIMIT) -> Win:
    """The mount at the + limit margin with the prompt up."""
    at(fake, x0, wmin=x0 - 180.0)
    time.sleep(0.4)  # the poller reads the new pose
    w = Win(tc)
    assert not w.send("jog", {"axis": 0, "dir": 1, "speed": 10.0}, "lim")["ok"]
    wait_for(lambda: snap(tc)["jog_limit"] is not None)
    fake.calls.clear()
    return w


def test_wrap_turn_goes_to_the_same_azimuth_through_the_other_representation(tmp_path):
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, app):
        w = limit_state(tc, fake)
        ack = w.send("wrap_turn", None, "t")
        assert ack["ok"], ack
        to = AT_LIMIT - 360.0
        assert paths(fake) == ["/mount/tracking_off", WRAP, "/mount/goto_alt_az"]
        assert wraps(fake) == [to - 180.0]
        goto = [q for p, q in fake.writes if p == "/mount/goto_alt_az"][0]
        assert float(goto["alt_degs"]) == pytest.approx(45.0)
        assert float(goto["az_degs"]) == pytest.approx(AT_LIMIT % 360.0)
        wait_for(lambda: snap(tc)["turn"] is not None)
        s = snap(tc)
        assert s["turn"] == {"from_deg": AT_LIMIT, "to_deg": to}
        assert s["jog_limit"] is None
        # the monitor holds the corridor plan of a validated goto
        plan = app.state.monitor.plan
        assert plan is not None and plan.target == (to, 45.0)
        # arrival ends it, STOPPED
        at(fake, to, wmin=to - 180.0, mount__is_slewing=False)
        wait_for(lambda: snap(tc)["turn"] is None, timeout=4.0)
        wait_for(lambda: snap(tc)["state"] == "STOPPED")
        w.close()
    ev = audit_lines(tmp_path)
    t = [e for e in ev if e["event"] == "wrap_turn"]
    assert len(t) == 1 and t[0]["from_deg"] == AT_LIMIT and t[0]["to_deg"] == to
    assert t[0]["dir"] == 1 and t[0]["est_s"] > 0
    end = [e for e in ev if e["event"] == "wrap_turn_end"]
    assert len(end) == 1 and end[0]["how"] == "done" and end[0]["final_deg"] == to


def test_wrap_turn_negative_direction_and_no_limit_prompt_needed():
    fake = fresh(FakePwi4(), x0=-291.0, wmin=-471.0)
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("wrap_turn", None, "t")["ok"]  # +360 fits, -360 does not
        assert wraps(fake) == [69.0 - 180.0]
        wait_for(lambda: snap(tc)["turn"] is not None)
        assert snap(tc)["turn"]["to_deg"] == 69.0
        w.send("stop", None, "s")
        w.close()
    fake = fresh(FakePwi4())  # pos 120: only -240 fits
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("wrap_turn", None, "t")["ok"]
        wait_for(lambda: snap(tc)["turn"] is not None)
        assert snap(tc)["turn"]["to_deg"] == -240.0
        w.send("stop", None, "s")
        w.close()


def test_wrap_turn_no_room_refused():
    fake = fresh(FakePwi4(), x0=0.0)  # +-360 are both outside +-300
    with running(fake) as (tc, _, _):
        w = Win(tc)
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_NO_ROOM == "No room for a 360° turn"
        assert ack["code"] == "no_room"
        d = check(tc, "wrap_turn", {})
        assert not d["ok"] and d["reason"] == R_NO_ROOM and d["code"] == "no_room"
        assert paths(fake) == []
        w.close()


def test_wrap_turn_uses_the_limit_direction_and_refuses_when_that_way_has_no_room():
    # prompt at the + limit but the axis sits where -360 does not fit (cannot happen at the real
    # limit; guards the plan): dir is the prompt's, never the other way
    fake = fresh(FakePwi4(), x0=AT_LIMIT)
    with running(fake) as (tc, _, app):
        w = limit_state(tc, fake)
        app.state.control.jogc.limit.dir = -1  # pretend the prompt is for the - limit
        # -dir*360 = +360 from 291.5 does not fit
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_NO_ROOM
        w.close()


def test_wrap_turn_guards():
    # not homed
    fake = fresh(FakePwi4())
    with running(fake, homed=False) as (tc, _, _):
        w = Win(tc)
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_NOT_HOMED and ack["code"] == "not_homed"
        assert paths(fake) == []
        w.close()
    # stalled
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, app):
        app.state.control.homed.stall(0)
        w = Win(tc)
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_STALLED and ack["code"] == "stalled"
        w.close()
    # axis disabled
    fake = fresh(FakePwi4(), mount__axis0__is_enabled=False)
    with running(fake) as (tc, _, _):
        w = Win(tc)
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == "Axis 0 disabled"
        w.close()
    # Lim EL
    fake = fresh(FakePwi4(), mount__altitude_degs=10.0, mount__axis1__position_degs=10.0)
    with running(fake) as (tc, _, app):
        w = Win(tc)
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == r_below(app.state.safety.lim_el())
        assert ack["code"] == "below_lim_el"
        w.close()
    # another window holds control
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        w1, w2 = Win(tc), Win(tc)
        assert w1.send("take_control", None, "tc")["ok"]
        ack = w2.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_OTHER_HAS_CONTROL
        w1.close()
        w2.close()


def test_wrap_turn_sun_target_and_sun_path_refused(monkeypatch):
    # the pose itself inside the Sun disc: same az/el = Sun target
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,120")
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        w = Win(tc)
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_SUN and ack["code"] == "sun_target"
        w.close()
    # Sun on the 360 deg sweep (120 -> -240 passes az 300 = -60), far from the pose itself
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,300")
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        d = check(tc, "wrap_turn", {})
        assert not d["ok"] and d["reason"] == R_PATH_SUN and d["code"] == "sun_path"
        w = Win(tc)
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_PATH_SUN
        assert paths(fake) == []
        w.close()


def test_wrap_turn_refused_while_jogging_or_turning_and_others_refused_while_turning():
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("jog", J0, "j")["ok"]
        ack = w.send("wrap_turn", None, "t")
        assert not ack["ok"] and ack["reason"] == R_JOGGING
        w.send("jog_stop", None, "r")
        wait_for(lambda: snap(tc)["jog"] is None)
        assert w.send("wrap_turn", None, "t2")["ok"]
        wait_for(lambda: snap(tc)["turn"] is not None)
        for name, args in (
            ("wrap_turn", None),
            ("goto_altaz", {"alt": 50.0, "az": 100.0}),
            ("jog", J0),
            ("step", {"axis": 0, "dir": 1, "size_arcmin": 5}),
            ("tracking", {"on": True}),
        ):
            ack = w.send(name, args, "x")
            assert not ack["ok"] and ack["reason"] == R_TURN == "Turn in progress; press STOP first"
        d = check(tc, "jog", J0)
        assert not d["ok"] and d["reason"] == R_TURN and d["code"] == "turn_in_progress"
        w.close()


def test_stop_cancels_the_turn(tmp_path):
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("wrap_turn", None, "t")["ok"]
        wait_for(lambda: snap(tc)["turn"] is not None)
        n = len(paths(fake))
        assert w.send("stop", None, "s")["ok"]
        assert "/mount/stop" in paths(fake)[n:]
        wait_for(lambda: snap(tc)["turn"] is None)
        assert app.state.monitor.plan is None
        w.close()
    end = [e for e in audit_lines(tmp_path) if e["event"] == "wrap_turn_end"]
    assert [e["how"] for e in end] == ["stop"]


def test_turn_that_stops_short_ends_as_stopped(tmp_path):
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("wrap_turn", None, "t")["ok"]
        wait_for(lambda: snap(tc)["turn"] is not None)
        fake.set(mount__axis0__dist_to_target_arcsec=36000, mount__is_slewing=False)
        # PWI4 stopped elsewhere: still far from the target
        wait_for(lambda: snap(tc)["turn"] is None, timeout=5.0)
        w.close()
    end = [e for e in audit_lines(tmp_path) if e["event"] == "wrap_turn_end"]
    assert [e["how"] for e in end] == ["stopped"]


def drive_turn(fake: FakePwi4, x0: float, x1: float, secs: float, bad_alt: float | None = None):
    """Move axis 0 x0 -> x1 over `secs` like a slewing mount; optionally leave the path."""

    def run() -> None:
        t0 = time.monotonic()
        while True:
            f = min(1.0, (time.monotonic() - t0) / secs)
            x = x0 + (x1 - x0) * f
            over: dict = {"mount__is_slewing": f < 1.0, "mount__azimuth_degs": x % 360.0}
            if bad_alt is not None and f > 0.4:
                over.update(mount__altitude_degs=bad_alt, mount__axis1__position_degs=bad_alt)
            fake.set(mount__axis0__position_degs=x, **over)
            if f >= 1.0 or any(p == "/mount/stop" for p, _ in fake.writes):
                return
            time.sleep(0.02)

    th = threading.Thread(target=run)
    th.start()
    return th


def test_monitor_does_not_stop_a_turn_on_its_path_but_stops_one_that_leaves_it(tmp_path):
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("wrap_turn", None, "t")["ok"]
        th = drive_turn(fake, 120.0, -240.0, 3.0)
        th.join(6)
        assert "/mount/stop" not in paths(fake)
        wait_for(lambda: snap(tc)["turn"] is None, timeout=4.0)
        assert app.state.monitor.stops == 0
        w.close()
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("wrap_turn", None, "t")["ok"]
        th = drive_turn(fake, 120.0, -240.0, 3.0, bad_alt=55.0)
        th.join(6)
        wait_for(lambda: "/mount/stop" in paths(fake), timeout=3.0)
        wait_for(lambda: snap(tc)["turn"] is None)
        w.close()
    ev = audit_lines(tmp_path)
    assert any(e["event"] == "limit_stop" and "validated slew path" in e["reason"] for e in ev)


def test_dry_check_wrap_turn_is_read_only():
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        d = check(tc, "wrap_turn", {})
        assert d["ok"] and d["reason"] is None
        assert d["target"]["alt"] == pytest.approx(45.0)
        assert d["target"]["az"] == pytest.approx(120.0)
        r = tc.post("/api/mount/check", json={"cmd": "wrap_turn", "args": {"x": 1}}, headers=H)
        assert r.status_code == 422
        assert paths(fake) == []
        assert snap(tc)["turn"] is None


# ------------------------------------------------------------------ cancel ("stay")


def test_wrap_turn_cancel_clears_the_prompt_and_is_audited(tmp_path):
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        w = limit_state(tc, fake)
        ack = w.send("wrap_turn_cancel", None, "c")
        assert ack["ok"]
        wait_for(lambda: snap(tc)["jog_limit"] is None)
        assert paths(fake) == []  # the mount is already stopped: no PWI4 call
        assert w.send("wrap_turn_cancel", None, "c2")["ok"]  # nothing to cancel: still fine
        # the next jog that way prompts again
        assert not w.send("jog", {"axis": 0, "dir": 1, "speed": 10.0}, "j")["ok"]
        wait_for(lambda: snap(tc)["jog_limit"] is not None)
        w.close()
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "wrap_turn_cancel"]
    assert [e["had_limit"] for e in ev] == [True, False]


def test_wrap_turn_cancel_refused_for_a_window_without_control():
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        w1, w2 = Win(tc), Win(tc)
        assert w1.send("take_control", None, "tc")["ok"]
        ack = w2.send("wrap_turn_cancel", None, "c")
        assert not ack["ok"] and ack["reason"] == R_OTHER_HAS_CONTROL
        w1.close()
        w2.close()


# ------------------------------------------------------------------ snapshot, estimate


def test_snapshot_fields_null_by_default_and_when_disconnected():
    fake = fresh(FakePwi4())
    with running(fake) as (tc, _, _):
        body = snap(tc)
        assert body["jog_limit"] is None and body["turn"] is None
        w = limit_state(tc, fake)
        assert snap(tc)["jog_limit"] is not None
        fake.set(mount__is_connected=False)
        wait_for(lambda: snap(tc)["state"] == "DISCONNECTED")
        assert snap(tc)["jog_limit"] is None and snap(tc)["turn"] is None
        w.close()


def test_turn_estimate_scales_with_speed_and_includes_the_approach():
    fast = turn_duration_s(0.0, -360.0, 20.0, 20.0, 0.5)
    slow = turn_duration_s(0.0, -360.0, 5.0, 20.0, 0.5)
    assert 360.0 / 20.0 < fast < 30.0  # cruise time plus accel and approach
    assert slow > 360.0 / 5.0
    assert slow > fast


def test_offsets_helper_untouched_by_slide():
    """A slide is not an offset write: the jog's own offset calls stay exactly two."""
    fake = fresh(FakePwi4(), x0=150.0)
    args = {"axis": 0, "dir": 1, "speed": 2.0}
    with running(fake) as (tc, _, _):
        w = Win(tc)
        assert w.send("jog", args, "j")["ok"]
        wait_for(lambda: wraps(fake) != [], timeout=2.0)
        assert len(offsets(fake)) == 1
        w.send("jog_stop", None, "r")
        w.close()
