"""T3.3: the not-homed guard (Q3/Q4), the `homed_override` command, TRACK refusals, and the
`homed` snapshot field. All against FakePwi4 (no real PWI4 is contacted).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway.audit import AuditLog
from gateway.commands import CommandError, parse_command
from gateway.homed import UNREACHABLE_END_S, HomedGuard
from gateway.pwi4_io import PollResult

from .conftest import FakePwi4
from .test_control import Win, audit_lines, cmd, running, wait_for
from .test_sequences import (
    SUN_ON_HOME_PATH,
    disconnected_fake,
    home_fast,
    paths,
    virtual,
    wait_seq,
)

NOT_HOMED = "Mount not homed"


def homed_of(app) -> dict | None:
    snap = app.state.poller.latest
    return None if snap is None or snap.homed is None else snap.homed.model_dump()


def homed_state(app) -> str | None:
    h = homed_of(app)
    return h["state"] if h else None


def events(tmp_path, name: str) -> list[dict]:
    return [e for e in audit_lines(tmp_path) if e["event"] == name]


# ------------------------------------------------------------------ schema


def test_homed_override_schema():
    assert parse_command(cmd("homed_override")).name == "homed_override"
    assert parse_command(cmd("homed_override")).args == {}
    with pytest.raises(CommandError):
        parse_command(cmd("homed_override", {"x": 1}))


# ------------------------------------------------------------------ guard unit tests


def res(connected: bool | None) -> PollResult:
    """connected None = PWI4 unreachable."""
    if connected is None:
        return PollResult(False, error="down", kind="unreachable")
    return PollResult(True, status=SimpleNamespace(mount=SimpleNamespace(is_connected=connected)))


def guard(tmp_path) -> HomedGuard:
    return HomedGuard(AuditLog(tmp_path / "audit"))


def test_guard_session_causes_and_clearing(tmp_path):
    g = guard(tmp_path)
    assert g.info() is None
    g.observe(res(True), 1.0)  # first observation after the gateway start: connected already
    assert g.state == "not_homed" and g.blocking and g.info().since.endswith("Z")
    g.observe(res(True), 1.1)  # no new session while connected
    g.home_completed("completed")
    assert g.state == "homed" and not g.blocking
    g.observe(res(True), 2.0)
    assert g.state == "homed"
    g.observe(res(False), 3.0)  # disconnected: session ends
    assert g.info() is None
    g.observe(res(True), 4.0)  # connected from PWI4's own GUI
    assert g.state == "not_homed"
    assert g.override("w1") and g.state == "overridden"
    assert not g.override("w1")  # only from not_homed
    g.observe(res(True), 5.0)
    assert g.state == "overridden"  # stays cleared for the whole session
    causes = [e["cause"] for e in events(tmp_path, "homed_session_start")]
    assert causes == ["gateway_start", "mount_connected_externally"]
    assert [e["how"] for e in events(tmp_path, "homed")] == ["completed"]
    assert len(events(tmp_path, "homed_override")) == 1


def test_guard_pwi4_unreachable_blip_vs_reconnect(tmp_path):
    g = guard(tmp_path)
    g.observe(res(True), 0.0)
    g.home_completed("no_motion")
    g.observe(res(None), 1.0)
    g.observe(res(None), 1.0 + UNREACHABLE_END_S - 0.5)  # a blip: the session survives
    g.observe(res(True), 3.5)
    assert g.state == "homed"
    g.observe(res(None), 10.0)
    g.observe(res(None), 10.0 + UNREACHABLE_END_S)  # long outage: session over
    assert g.info() is None
    g.observe(res(True), 20.0)  # PWI4 is back with the mount connected: not homed (Q4)
    assert g.state == "not_homed"
    assert [e["cause"] for e in events(tmp_path, "homed_session_start")][-1] == "pwi4_reconnect"


def test_guard_connect_sequence_suppresses_observed_start(tmp_path):
    g = guard(tmp_path)
    g.connecting = lambda: True
    g.observe(res(True), 0.0)
    assert g.info() is None  # the CONNECT sequence starts the session itself
    g.begin("connect_sequence")
    assert g.state == "not_homed"


def test_home_completed_without_session_opens_one(tmp_path):
    g = guard(tmp_path)
    g.home_completed("completed")
    assert g.state == "homed"


# ------------------------------------------------------------------ gateway: found connected


def test_found_connected_mount_is_not_homed_and_refuses_motion(tmp_path):
    with running(homed=False) as (tc, fake, app):
        wait_for(lambda: homed_state(app) == "not_homed")
        h = homed_of(app)
        assert set(h) == {"state", "since", "reason"} and h["since"].endswith("Z")
        assert h["reason"] is None  # T3.3b: only the stall guard sets a reason
        body = tc.get("/api/mount/status").json()
        assert body["homed"]["state"] == "not_homed"
        w = Win(tc)
        for name, args in [
            ("goto_altaz", {"alt": 45, "az": 125}),
            ("goto_radec_j2000", {"ra_h": 3.0, "dec_deg": 10.0}),
            ("tracking", {"on": True}),
        ]:
            ack = w.send(name, args)
            assert ack["ok"] is False and ack["reason"] == NOT_HOMED, (name, ack)
        assert fake.writes == []
        # always allowed: tracking off, stop, take / release control
        assert w.send("tracking", {"on": False}, "t2")["ok"]
        assert w.send("stop", cid="s1")["ok"]
        assert w.send("take_control", cid="c3")["ok"]
        assert w.send("release_control", cid="c4")["ok"]
        w.close()
    ev = [e["cause"] for e in events(tmp_path, "homed_session_start")]
    assert ev == ["gateway_start"]


def test_home_completion_clears_the_guard(tmp_path):
    with running(homed=False) as (tc, fake, app):
        virtual(app)
        wait_for(lambda: homed_state(app) == "not_homed")
        w = Win(tc)
        assert w.send("home")["ok"]  # home is never gated
        assert wait_seq(app).status == "done"
        wait_for(lambda: homed_state(app) == "homed")
        assert w.send("goto_altaz", {"alt": 45, "az": 125}, "g1")["ok"]
        w.close()
    assert [e["how"] for e in events(tmp_path, "homed")] == ["no_motion"]


def test_failed_home_leaves_the_guard_on(tmp_path):
    fake = FakePwi4()
    fake.fail_writes.add("/mount/find_home")
    with running(fake, homed=False) as (tc, _, app):
        virtual(app)
        wait_for(lambda: homed_state(app) == "not_homed")
        w = Win(tc)
        assert w.send("home")["ok"]
        assert wait_seq(app).status == "failed"
        assert homed_state(app) == "not_homed"
        w.close()
    assert events(tmp_path, "homed") == []


def test_cancelled_home_leaves_the_guard_on():
    fake = FakePwi4()
    with running(fake, homed=False) as (tc, _, app):
        wait_for(lambda: homed_state(app) == "not_homed")
        fake.write_delay = 0.4  # the sequence is slow: STOP cancels it meanwhile
        w = Win(tc)
        assert w.send("home")["ok"]
        assert w.send("stop", cid="s1")["ok"]
        wait_seq(app)
        assert seq_status(app) == "cancelled"
        assert homed_state(app) == "not_homed"
        w.close()


def seq_status(app) -> str:
    return app.state.control.seq.info.status


# ------------------------------------------------------------------ override


def test_override_allows_motion_and_is_audited(tmp_path):
    with running(homed=False) as (tc, fake, app):
        wait_for(lambda: homed_state(app) == "not_homed")
        w = Win(tc)
        ack = w.send("homed_override")
        assert ack["ok"] and ack["reason"] is None
        wait_for(lambda: homed_state(app) == "overridden")
        assert w.send("goto_altaz", {"alt": 45, "az": 125}, "g1")["ok"]
        # not accepted any more once cleared
        again = w.send("homed_override", cid="o2")
        assert again["ok"] is False and again["reason"] == "Mount is not in the not-homed state"
        w.close()
    assert len(events(tmp_path, "homed_override")) == 1
    cmds = [e for e in events(tmp_path, "cmd") if e["cmd"] == "homed_override"]
    assert [c["result"] for c in cmds] == ["ok", "refused"]


def test_override_needs_the_lock_and_a_connected_mount():
    with running(homed=False) as (tc, fake, app):
        wait_for(lambda: homed_state(app) == "not_homed")
        a, b = Win(tc), Win(tc)
        assert a.send("take_control")["ok"]
        ack = b.send("homed_override", cid="o1")
        assert ack["ok"] is False and ack["reason"] == "Another window has control"
        assert homed_state(app) == "not_homed"
        assert a.send("homed_override", cid="o2")["ok"]
        a.close()
        b.close()
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        w = Win(tc)
        ack = w.send("homed_override")
        assert ack["ok"] is False and ack["reason"] == "Mount not connected"
        w.close()


def test_override_takes_the_lock_when_free():
    with running(homed=False) as (tc, fake, app):
        wait_for(lambda: homed_state(app) == "not_homed")
        w = Win(tc)
        assert app.state.control.holder is None
        assert w.send("homed_override")["ok"]
        assert app.state.control.holder is not None
        w.close()


# ------------------------------------------------------------------ session lifecycle


def test_disconnect_ends_the_session_and_reconnect_starts_a_new_one():
    with running() as (tc, fake, app):
        w = Win(tc)
        wait_for(lambda: homed_state(app) == "overridden")
        home_fast(fake, app)
        assert w.send("disconnect")["ok"]
        wait_seq(app)
        wait_for(lambda: app.state.poller.latest.state == "DISCONNECTED")
        assert homed_of(app) is None
        assert tc.get("/api/mount/status").json()["homed"] is None
        w.close()


def test_mount_connected_outside_the_gateway_is_a_new_session(tmp_path):
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        assert homed_of(app) is None
        fake.set(
            mount__is_connected=True, mount__axis0__is_enabled=True, mount__axis1__is_enabled=True
        )
        wait_for(lambda: homed_state(app) == "not_homed")
        fake.set(mount__is_connected=False)
        wait_for(lambda: homed_of(app) is None)
    causes = [e["cause"] for e in events(tmp_path, "homed_session_start")]
    assert causes == ["mount_connected_externally"]


def test_connect_sequence_homes_and_clears(tmp_path):
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        home_fast(fake, app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        wait_seq(app)
        assert seq_status(app) == "done"
        wait_for(lambda: homed_state(app) == "homed")
        w.close()
    causes = [e["cause"] for e in events(tmp_path, "homed_session_start")]
    assert causes == ["connect_sequence"]  # not also "observed"
    assert [e["how"] for e in events(tmp_path, "homed")] == ["no_motion"]


def test_connect_with_home_blocked_by_sun_stays_not_homed(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", SUN_ON_HOME_PATH)
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        info = wait_seq(app)
        assert info.status == "done" and info.warning
        assert "/mount/find_home" not in paths(fake)
        wait_for(lambda: homed_state(app) == "not_homed")
        ack = w.send("goto_altaz", {"alt": 45, "az": 125}, "g1")
        assert ack["ok"] is False and ack["reason"] == NOT_HOMED
        assert w.send("homed_override", cid="o1")["ok"]
        wait_for(lambda: homed_state(app) == "overridden")
        assert w.send("goto_altaz", {"alt": 45, "az": 125}, "g2")["ok"]
        w.close()
    assert len(events(tmp_path, "homed_override")) == 1


def test_connect_on_an_already_connected_mount_ends_homed():
    with running(homed=False) as (tc, fake, app):
        virtual(app)
        wait_for(lambda: homed_state(app) == "not_homed")
        w = Win(tc)
        assert w.send("connect")["ok"]
        wait_seq(app)
        assert homed_state(app) == "homed"
        w.close()


def test_snapshot_homed_is_null_when_pwi4_is_down():
    with running(homed=False) as (tc, fake, app):
        wait_for(lambda: homed_state(app) == "not_homed")
        fake.down = True
        wait_for(lambda: app.state.poller.latest.state == "DISCONNECTED")
        assert app.state.poller.latest.homed is None
        assert json.loads(app.state.poller.latest.model_dump_json())["homed"] is None


# ------------------------------------------------------------------ TRACK


def test_tracking_on_refused_while_slewing(tmp_path):
    class Moving(FakePwi4):
        n = 0
        moving = False

        async def handler(self, request):
            if self.moving and request.url.path == "/status":
                self.n += 1
                p = 120.0 + 0.5 * self.n
                self.set(
                    mount__is_slewing=True,
                    mount__axis0__position_degs=p,
                    mount__azimuth_degs=p,
                )
            return await super().handler(request)

    fake = Moving()
    with running(fake) as (tc, _, app):
        fake.moving = True
        wait_for(lambda: app.state.poller.latest.state == "SLEWING")
        w = Win(tc)
        ack = w.send("tracking", {"on": True})
        assert ack["ok"] is False and ack["reason"] == "Mount is slewing; press STOP first"
        assert ("/mount/tracking_on", {}) not in fake.writes
        assert w.send("tracking", {"on": False}, "t2")["ok"]  # tracking off stays allowed
        w.close()


def test_tracking_on_ok_audits_track_start(tmp_path):
    with running() as (tc, fake, app):
        w = Win(tc)
        assert w.send("tracking", {"on": True})["ok"]
        assert ("/mount/tracking_on", {}) in fake.writes
        w.close()
    ts = events(tmp_path, "track_start")
    assert len(ts) == 1
    for k in ("ra_h", "dec_deg", "alt", "az"):
        assert ts[0][k] is not None
    assert ts[0]["alt"] == 45.0 and ts[0]["az"] == 120.0


def test_tracking_on_refusals_below_lim_el_and_in_sun_disc(monkeypatch, tmp_path):
    fake = FakePwi4()
    fake.set(mount__altitude_degs=5.0, mount__axis1__position_degs=5.0)
    with running(fake) as (tc, _, app):
        w = Win(tc)
        ack = w.send("tracking", {"on": True})
        assert ack["ok"] is False and ack["reason"].startswith("Below station limit elevation")
        w.close()
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,125")  # 4.x deg from the fake pointing 45/120
    with running() as (tc, fake, app):
        w = Win(tc)
        ack = w.send("tracking", {"on": True})
        assert ack["ok"] is False and ack["reason"] == "Within 15° of the Sun"
        assert fake.writes == []
        w.close()
    refusals = events(tmp_path, "safety_refusal")
    assert len(refusals) >= 1 and all(r["cmd"] == "tracking" for r in refusals)


def test_tracking_on_is_sent_as_is_without_wrap_centring(tmp_path):
    """T3.3b (P19 revert): after a stopped wrap-changing goto the axis can lie outside PWI4's wrap
    window; PWI4's own unwinding turn on tracking_on is intended, so TRACK must not touch the
    wrap window (the T3.3 centring was removed)."""
    fake = FakePwi4()
    fake.set(
        mount__azimuth_degs=200.0,
        mount__axis0__position_degs=200.0,
        mount__axis0_wrap_range_min_degs=-229.44,
    )
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("tracking", {"on": True})["ok"]
        p = paths(fake)
        assert "/mount/tracking_on" in p
        assert "/mount/set_axis0_wrap_range_min" not in p
        w.close()
