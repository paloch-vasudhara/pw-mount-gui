"""T3.5c: D-SUN wiring (safety -> control executor -> monitor -> state / snapshot / audit / HOME).

All against FakePwi4. The fake mount does not move on its own, so a test hook replaces the
executor's wait (`ControlService.route_sleep`): it records the requested wait, optionally parks the
fake mount at the route end (what a real path does) and returns after a short real sleep.
The planner itself is covered by test_dsun.py; the PWI4-simulator runs by test_dsun_pwi4sim.py.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from gateway import dsun
from gateway.control import ROUTE_SETTLE_S, route_wrap_plan
from gateway.safety import R_NO_DETOUR, R_PATH_SUN, ValidatedGoto, validated_decision
from gateway.snapshot import jd_from_utc

from .conftest import FakePwi4
from .test_control import TOKEN, Win, audit_lines, running, wait_for

H = {"x-mount-token": TOKEN, "origin": "http://127.0.0.1:3200"}
SUN = "45,150"  # the fake mount points at alt 45 / az 120: az 180 is 30 deg beyond the Sun


def park_fake(fake: FakePwi4, axis0: float, alt: float) -> None:
    fake.set(
        mount__azimuth_degs=round(axis0 % 360.0, 4),
        mount__axis0__position_degs=round(axis0, 4),
        mount__altitude_degs=round(alt, 4),
        mount__axis1__position_degs=round(alt, 4),
    )


def install_hook(app, fake, waits, *, park=True, real_s=0.4, block=False):
    """route_sleep seam: record the wait; park the fake at the route end; sleep briefly."""

    async def hook(seconds: float) -> None:
        waits.append(seconds)
        if block:
            await asyncio.sleep(60.0)
        plan = app.state.monitor.plan
        if park and plan is not None and plan.route:
            park_fake(fake, plan.target[0], plan.target[1])
        await asyncio.sleep(real_s)

    app.state.control.route_sleep = hook


def snapshot(tc) -> dict:
    return tc.get("/api/mount/status").json()


def snap_when(tc, pred, timeout=3.0) -> dict:
    """The newest snapshot once `pred(snapshot)` holds (the poller republishes at 10 Hz)."""
    end = time.monotonic() + timeout
    while True:
        s = snapshot(tc)
        if pred(s) or time.monotonic() > end:
            return s
        time.sleep(0.03)


def paths(fake) -> list[str]:
    return [p for p, _ in fake.writes]


def check(tc, cmd: str, args: dict) -> dict:
    r = tc.post("/api/mount/check", json={"cmd": cmd, "args": args}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _sun(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", SUN)


# ------------------------------------------------------------------ safety / dry run


def test_dry_check_reports_detour_and_writes_nothing():
    with running() as (tc, fake, app):
        d = check(tc, "goto_altaz", {"alt": 45.0, "az": 180.0})
        assert d["ok"] is True and d["code"] == "sun_detour" and d["reason"] is None
        assert d["detour"]["side"] in ("cw", "ccw")
        assert d["detour"]["duration_s"] > dsun.LEAD_S + ROUTE_SETTLE_S + 5
        assert fake.writes == [] and app.state.safety._routes == {}
        # a clear slew has no detour
        d = check(tc, "goto_altaz", {"alt": 45.0, "az": 100.0})
        assert d["code"] is None and d["detour"] is None


def test_no_detour_refusal_code():
    fake = FakePwi4()
    fake.set(
        mount__altitude_degs=30.0, mount__axis1__position_degs=30.0,
        mount__axis1__max_mech_position_degs=40.0, mount__azimuth_degs=130.0,
        mount__axis0__position_degs=130.0,
    )  # fmt: skip
    import os

    os.environ["PW_TEST_SUN_ALTAZ"] = "30,180"
    with running(fake) as (tc, _, _):
        d = check(tc, "goto_altaz", {"alt": 30.0, "az": 230.0})
        assert d["ok"] is False and d["reason"] == R_NO_DETOUR and d["code"] == "sun_no_detour"
        assert d["detour"] is None
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 30.0, "az": 230.0})["reason"] == R_NO_DETOUR
        assert fake.writes == []
        w.close()


def test_start_inside_disc_keeps_path_refusal():
    import os

    os.environ["PW_TEST_SUN_ALTAZ"] = "45,125"  # 3.5 deg from the mount
    with running() as (tc, fake, _):
        d = check(tc, "goto_altaz", {"alt": 45.0, "az": 200.0})
        assert d["reason"] == R_PATH_SUN and d["code"] == "sun_path"


# ------------------------------------------------------------------ executor


def test_routed_goto_sequence_audit_and_snapshot(tmp_path):
    with running() as (tc, fake, app):
        waits: list[float] = []
        install_hook(app, fake, waits, real_s=1.2)
        w = Win(tc)
        t_ack = time.time()
        ack = w.send("goto_altaz", {"alt": 45, "az": 180})
        assert ack["ok"], ack
        # acked at the route start, mid-route state is SLEWING / "Avoiding the Sun"
        snap = snap_when(tc, lambda s: s["route"] is not None)
        assert snap["state"] == "SLEWING" and snap["state_reason"] == "Avoiding the Sun"
        assert snap["route"]["active"] is True and snap["route"]["sides"][0] in ("cw", "ccw")
        assert snap["route"]["t_end"].endswith("Z")
        mon = app.state.monitor
        assert mon.plan is not None and mon.plan.route is True
        wait_for(lambda: "/mount/goto_alt_az" in paths(fake), 6)
        wait_for(lambda: app.state.control.route is None)
        seq = paths(fake)
        assert seq == [
            "/mount/tracking_off",
            "/mount/set_axis0_wrap_range_min",
            "/mount/custom_path/new",
            "/mount/custom_path/add_point_list",
            "/mount/custom_path/apply",
            "/mount/tracking_off",
            "/mount/set_axis0_wrap_range_min",  # the final goto's own nearest-wrap preparation
            "/mount/goto_alt_az",
        ], seq
        calls = dict(fake.writes)
        assert calls["/mount/custom_path/new"] == {"type": "altaz"}
        assert calls["/mount/custom_path/apply"] == {"update_wrap": "0"}
        assert calls["/mount/goto_alt_az"] == {"alt_degs": "45.0", "az_degs": "180.0"}
        # points: first = the current pose at now + LEAD, then ~0.5 s apart, jd absolute
        body = dict(fake.bodies)["/mount/custom_path/add_point_list"]
        assert body.startswith("data=")
        from urllib.parse import unquote_plus

        lines = unquote_plus(body[5:]).splitlines()
        jd, az, alt = (float(x) for x in lines[0].split(","))
        assert (az, alt) == (120.0, 45.0)
        now_jd = jd_from_utc(datetime.now(UTC))
        assert 0.0 < (jd - now_jd) * 86400.0 < dsun.LEAD_S + 1.0 + (time.time() - t_ack)
        jd1 = float(lines[1].split(",")[0])
        assert (jd1 - jd) * 86400.0 == pytest.approx(dsun.POINT_DT_S, abs=0.01)
        last = lines[-1].split(",")
        assert float(last[1]) == pytest.approx(180.0, abs=0.01) and float(last[2]) == 45.0
        assert len(lines) > 20
        # the wait = LEAD + route + settle (minus the upload time)
        route_s = (float(lines[-1].split(",")[0]) - jd) * 86400.0
        assert waits and waits[0] == pytest.approx(dsun.LEAD_S + route_s + ROUTE_SETTLE_S, abs=1.0)
        # the wrap window holds the whole route with the margin
        wrap = float(fake.writes[1][1]["degs"])
        assert wrap == pytest.approx(120.0 - 1.0, abs=0.01)
        # state is released after the route
        assert snap_when(tc, lambda s: s["route"] is None)["route"] is None
        assert (
            mon.plan is None or mon.plan.route is False
        )  # route plan gone (the final goto's, if any)
        w.close()
    ev = {e["event"]: e for e in audit_lines(tmp_path)}
    d = ev["sun_detour"]
    assert d["kind"] == "goto_altaz" and d["sides"][0] in ("cw", "ccw") and d["min_sun_sep"] >= 15.9
    assert d["start"] == [120.0, 45.0] and d["duration_s"] > 5 and d["detour_deg"] > 0
    assert "sun_detour_done" in ev and "sun_detour_stopped" not in ev


def test_force_route_when_only_the_predictor_flags_the_slew(monkeypatch):
    # the straight axis-space line is clear (the planner returns None) but the predictor's
    # per-axis dog-leg crosses the disc: fly the line as a route, never the plain goto
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "21,143")
    fake = FakePwi4()
    park_fake(fake, 54.0, 47.0)
    with running(fake) as (tc, _, app):
        d = check(tc, "goto_altaz", {"alt": 31.0, "az": 199.0})
        assert d["code"] == "sun_detour" and d["detour"]["side"] == "straight", d
        waits: list[float] = []
        install_hook(app, fake, waits)
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 31, "az": 199})["ok"]
        wait_for(lambda: "/mount/goto_alt_az" in paths(fake), 6)
        assert "/mount/custom_path/apply" in paths(fake)
        assert paths(fake).index("/mount/custom_path/apply") < paths(fake).index(
            "/mount/goto_alt_az"
        )
        w.close()


def test_celestial_goto_routed_ends_with_goto_ra_dec(monkeypatch):
    from .test_control import up_radec

    ra, dec = up_radec()
    with running() as (tc, fake, app):
        site = app.state.poller.pwi4_site()
        alt, az = app.state.ephem.radec_j2000_altaz(site, ra, dec)
        # the Sun halfway between the mount (alt 45 / az 120) and the target
        monkeypatch.setattr(
            app.state.safety, "sun_override", ((alt + 45.0) / 2.0, ((az + 120.0) / 2.0) % 360.0)
        )
        d = check(tc, "goto_radec_j2000", {"ra_h": ra, "dec_deg": dec})
        if d["code"] != "sun_detour":  # the geometry depends on the clock: nothing to test then
            pytest.skip(f"no crossing for this target: {d}")
        waits: list[float] = []
        install_hook(app, fake, waits)
        w = Win(tc)
        assert w.send("goto_radec_j2000", {"ra_h": ra, "dec_deg": dec})["ok"]
        wait_for(lambda: "/mount/goto_ra_dec_j2000" in paths(fake), 6)
        seq = paths(fake)
        assert seq.index("/mount/custom_path/apply") < seq.index("/mount/goto_ra_dec_j2000")
        assert "/mount/goto_alt_az" not in seq
        w.close()


def test_stop_during_route_cancels_and_clears(tmp_path):
    with running() as (tc, fake, app):
        install_hook(app, fake, [], block=True)
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 45, "az": 180})["ok"]
        assert snap_when(tc, lambda s: s["route"] is not None)["route"] is not None
        mon = app.state.monitor
        assert w.send("stop", cid="s")["ok"]
        wait_for(lambda: app.state.control.route is None)
        time.sleep(0.5)
        assert "/mount/goto_alt_az" not in paths(fake) and "/mount/stop" in paths(fake)
        snap = snap_when(tc, lambda s: s["route"] is None)
        assert snap["route"] is None and snap["state_reason"] != "Avoiding the Sun"
        assert snap["state"] != "SLEWING" or snap["state_reason"] is None
        assert mon.plan is None
        # a new motion command is accepted again
        assert w.send("goto_altaz", {"alt": 45, "az": 100}, "n")["ok"]
        w.close()
    ev = audit_lines(tmp_path)
    stopped = [e for e in ev if e["event"] == "sun_detour_stopped"]
    assert len(stopped) == 1 and "STOP" in stopped[0]["reason"]
    assert not [e for e in ev if e["event"] == "sun_detour_done"]


def test_commands_refused_while_routing():
    with running() as (tc, fake, app):
        install_hook(app, fake, [], block=True)
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 45, "az": 180})["ok"]
        n = len(fake.writes)
        for name, args in (("goto_altaz", {"alt": 45, "az": 100}), ("tracking", {"on": True})):
            ack = w.send(name, args, name)
            assert ack["ok"] is False and "STOP" in ack["reason"], ack
        assert len(fake.writes) == n
        assert w.send("stop", cid="s")["ok"]
        w.close()


def test_pwi4_error_during_upload_stops_and_fails_the_job(tmp_path):
    with running() as (tc, fake, app):
        fake.fail_writes.add("/mount/custom_path/apply")
        w = Win(tc)
        ack = w.send("goto_altaz", {"alt": 45, "az": 180})
        assert ack["ok"] is False and "HTTP 500" in ack["reason"], ack
        wait_for(lambda: "/mount/stop" in paths(fake))
        assert "/mount/goto_alt_az" not in paths(fake)
        assert app.state.control.route is None
        w.close()


def test_final_leg_through_the_sun_is_refused_and_stopped(tmp_path):
    with running() as (tc, fake, app):
        waits: list[float] = []
        install_hook(app, fake, waits, park=False)  # the mount never left: the final leg crosses
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 45, "az": 180})["ok"]
        wait_for(lambda: "/mount/stop" in paths(fake), 6)
        assert "/mount/goto_alt_az" not in paths(fake)
        w.close()
    stopped = [e for e in audit_lines(tmp_path) if e["event"] == "sun_detour_stopped"]
    assert stopped and "Sun" in stopped[0]["reason"]


# ------------------------------------------------------------------ monitor


def test_route_plan_is_not_ended_by_idle_or_arrival():
    """Without the route flag the plan ends after PLAN_STILL_S of no motion and the predictive
    rule would then stop the mount when it passes the disc; a route plan lives by the clock."""
    with running() as (tc, fake, app):
        install_hook(app, fake, [], block=True)
        w = Win(tc)
        assert w.send("goto_altaz", {"alt": 45, "az": 180})["ok"]
        mon = app.state.monitor
        time.sleep(2.2)  # > PLAN_STILL_S + 1 s: the idle rule would have ended a goto plan
        assert mon.plan is not None and mon.plan.route and mon.stops == 0
        assert mon.plan.duration_s > dsun.LEAD_S + ROUTE_SETTLE_S + 5
        assert w.send("stop", cid="s")["ok"] and mon.plan is None
        w.close()


def test_route_plan_rules_corridor_and_actual_limits():
    corridor = [(100.0 + i, 45.0) for i in range(0, 60, 2)]
    plan = ValidatedGoto((100.0, 45.0), (158.0, 45.0), [corridor], 40.0, None, route=True)
    sun = (45.0, 130.0)  # the route passes 16+ deg from it in a real plan; here just arithmetic
    assert validated_decision(45.0, (120.0, 45.0), plan, 15.0, None, 120.0) is None
    assert validated_decision(45.0, (120.0, 48.5), plan, 15.0, None, 120.0)[0].startswith(
        "Mount left"
    )
    assert validated_decision(45.0, (130.0, 45.0), plan, 15.0, sun, 130.0)[0] == (
        "Within 15° of the Sun"
    )


# ------------------------------------------------------------------ wrap handling


def _pts(xs, dt=0.5, alt=40.0):
    return [dsun.RoutePoint(i * dt, x % 360.0, alt, x) for i, x in enumerate(xs)]


def test_route_wrap_plan_fits_whole_route_with_margin():
    w, kept, trunc = route_wrap_plan(_pts([100.0 + 5 * i for i in range(40)]))
    assert (w, len(kept), trunc) == (99.0, 40, False)
    w, kept, trunc = route_wrap_plan(_pts([-20.0, -60.0, -100.0]))  # descending, crossing 0 / 360
    assert w == -101.0 and len(kept) == 3 and not trunc


def test_route_wrap_plan_truncates_a_full_turn_up_and_down():
    up = _pts([100.0 + i for i in range(361)])
    w, kept, trunc = route_wrap_plan(up)
    assert trunc and w == pytest.approx(98.0)
    assert kept[0].axis0 == 100.0 and kept[-1].axis0 <= w + 358.0 < up[len(kept)].axis0
    assert all(w + 1.0 <= p.axis0 <= w + 358.0 for p in kept)
    down = _pts([100.0 - i for i in range(361)])
    w, kept, trunc = route_wrap_plan(down)
    assert trunc and w == pytest.approx(100.0 + 2.0 - 359.0)
    assert kept[0].axis0 == 100.0 and all(w + 1.0 <= p.axis0 <= w + 358.0 for p in kept)
    assert kept[-1].axis0 == pytest.approx(100.0 - 357.0 + 0.0, abs=1.0)


def test_wrap_turn_is_routed_truncated_and_finished(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,30")  # on the 360 deg sweep of the turn
    with running() as (tc, fake, app):
        waits: list[float] = []
        install_hook(app, fake, waits)
        w = Win(tc)
        ack = w.send("wrap_turn", {})
        assert ack["ok"], ack
        wait_for(lambda: "/mount/goto_alt_az" in paths(fake), 8)
        calls = [(p, q) for p, q in fake.writes]
        wraps = [float(q["degs"]) for p, q in calls if p == "/mount/set_axis0_wrap_range_min"]
        # first the route window anchored on the start (descending turn: 120 -> -240) ...
        assert wraps[0] == pytest.approx(120.0 + 2.0 - 359.0, abs=0.01)
        # ... then the turn's own final window (target0 - 180) and the goto
        assert wraps[-1] == pytest.approx(-240.0 - 180.0, abs=0.01)
        sent = [ln for ln in dict(fake.bodies)["/mount/custom_path/add_point_list"].split("%0A")]
        assert 100 < len(sent) < 160  # truncated before the window edge
        assert paths(fake)[-1] == "/mount/goto_alt_az"
        w.close()
    ev = {e["event"]: e for e in audit_lines(tmp_path)}
    assert ev["sun_detour"]["kind"] == "wrap_turn" and ev["sun_detour"]["truncated"] is True


# ------------------------------------------------------------------ HOME (Q10)


def _home_env(monkeypatch):
    # home pose alt 85 / az 180; the mount at alt 45 / az 120. The Sun between them.
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "65,150")


def test_home_routed_only_when_a_home_completed(monkeypatch):
    _home_env(monkeypatch)
    with running() as (tc, fake, app):
        control, safety = app.state.control, app.state.safety
        assert control.homed.state == "overridden"
        v = safety.check_home(control.fresh_status())
        assert v.reason == R_PATH_SUN and v.sun_blocked and v.route is None
        control.homed.home_completed("completed")
        v = safety.check_home(control.fresh_status())
        assert v.reason is None and v.route is not None and v.route.duration_s > 5
        control.homed.stall(0)  # stalled: the pose is not trusted
        v = safety.check_home(control.fresh_status())
        assert v.sun_blocked and v.route is None
        control.homed.begin("test")  # a new session, not homed
        v = safety.check_home(control.fresh_status())
        assert v.sun_blocked and v.route is None


def test_home_sequence_flies_the_route_then_find_home(monkeypatch, tmp_path):
    _home_env(monkeypatch)
    with running() as (tc, fake, app):
        control = app.state.control
        control.homed.home_completed("completed")
        waits: list[float] = []
        install_hook(app, fake, waits)
        w = Win(tc)
        assert w.send("home", {})["ok"]
        wait_for(lambda: "/mount/find_home" in paths(fake), 10)
        seq = paths(fake)
        assert seq.index("/mount/custom_path/apply") < seq.index("/mount/find_home")
        assert waits and control.route is None
        wait_for(lambda: not control.seq.active, 10)
        assert control.seq.info.status == "done"
        w.close()
    ev = audit_lines(tmp_path)
    assert [e for e in ev if e["event"] == "sun_detour" and e["kind"] == "home"]
    assert [e for e in ev if e["event"] == "sun_detour_done"]
