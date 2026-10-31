"""T3.4a: POST /api/mount/check, the read-only dry run of a goto's verdict (Q6).

Every refusal reason must equal what the real command gets for the same state (one shared
verdict function); a check never writes to PWI4, never takes the lock, never audits.
"""

from __future__ import annotations

import types

import pytest

from gateway.control import (
    R_NO_MOUNT,
    R_NO_PWI4,
    R_NOT_HOMED,
    R_OTHER_HAS_CONTROL,
    R_SHUTTING,
    R_STALLED,
    R_STARTING,
)
from gateway.safety import R_NO_AZ, R_SITE, R_SUN, r_below

from .conftest import FakePwi4
from .test_control import TOKEN, Win, audit_lines, running, up_radec, wait_for

H = {"x-mount-token": TOKEN, "origin": "http://127.0.0.1:3200"}
ALTAZ = {"alt": 45.0, "az": 125.0}  # fake mount points alt 45 / az 120


def check(tc, cmd: str, args: dict) -> dict:
    r = tc.post("/api/mount/check", json={"cmd": cmd, "args": args}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def parity(tc, cmd: str, args: dict, reason: str | None, code: str | None = None) -> dict:
    """The dry run says `reason`; the real command from a fresh window says the same."""
    dry = check(tc, cmd, args)
    assert dry["reason"] == reason, dry
    assert dry["ok"] is (reason is None)
    if code is not None:
        assert dry["code"] == code, dry
    w = Win(tc)
    ack = w.send(cmd, args, "real")
    assert ack["reason"] == reason, (dry, ack)
    w.close()
    return dry


@pytest.fixture(autouse=True)
def _no_sun(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")


# ------------------------------------------------------------------ auth / schema


def test_check_requires_token_and_origin():
    with running() as (tc, fake, _):
        body = {"cmd": "goto_altaz", "args": ALTAZ}
        assert tc.post("/api/mount/check", json=body).status_code == 401
        bad_token = {**H, "x-mount-token": "x"}
        assert tc.post("/api/mount/check", json=body, headers=bad_token).status_code == 401
        bad_origin = {**H, "origin": "http://evil.example"}
        assert tc.post("/api/mount/check", json=body, headers=bad_origin).status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"cmd": "nope", "args": {}},
        {"cmd": "stop", "args": {}},  # not a checkable command
        {"cmd": "tracking", "args": {"on": True}},
        {"cmd": "goto_altaz", "args": {"alt": 45}},  # missing az
        {"cmd": "goto_altaz", "args": {"alt": "45", "az": 1}},  # strict: no string coercion
        {"cmd": "goto_altaz", "args": {"alt": 95, "az": 1}},
        {"cmd": "goto_altaz", "args": {"alt": 45, "az": 360}},
        {"cmd": "goto_altaz", "args": {"alt": 45, "az": 1, "x": 1}},
        {"cmd": "goto_radec_j2000", "args": {"ra_h": 24, "dec_deg": 0}},
        {"cmd": "goto_radec_j2000", "args": {"ra_h": 1, "dec_deg": 91}},
        {"cmd": "goto_altaz"},
        {"args": {}},
        {"cmd": "goto_altaz", "args": {"alt": 45, "az": 1}, "extra": 1},
    ],
)
def test_check_bad_input_is_422(body):
    with running() as (tc, fake, _):
        r = tc.post("/api/mount/check", json=body, headers=H)
        assert r.status_code == 422, r.text
        assert fake.writes == []


# ------------------------------------------------------------------ ok + target


def test_ok_altaz_and_radec_with_observed_target():
    with running() as (tc, fake, app):
        d = check(tc, "goto_altaz", ALTAZ)
        assert d == {
            "ok": True,
            "reason": None,
            "code": None,
            "target": {"alt": 45.0, "az": 125.0},
            "detour": None,
        }
        ra, dec = up_radec()
        d = check(tc, "goto_radec_j2000", {"ra_h": ra, "dec_deg": dec})
        assert d["ok"] is True and d["reason"] is None
        site = app.state.poller.pwi4_site()
        alt, az = app.state.ephem.radec_j2000_altaz(site, ra, dec)
        assert d["target"]["alt"] == pytest.approx(alt, abs=0.02)
        assert d["target"]["az"] == pytest.approx(az, abs=0.02)
        assert fake.writes == []


def test_ok_matches_real_command():
    with running() as (tc, fake, _):
        d = parity(tc, "goto_altaz", ALTAZ, None)
        assert d["ok"] is True
        assert any(p.startswith("/mount/goto_alt_az") for p, _ in fake.writes)  # the REAL one


# ------------------------------------------------------------------ refusals == real command


def test_below_lim_el_and_sun_and_path(monkeypatch):
    with running() as (tc, fake, _):
        d = parity(tc, "goto_altaz", {"alt": 10.0, "az": 100.0}, r_below(15.0), "below_lim_el")
        assert d["target"] == {"alt": 10.0, "az": 100.0}
        ra, dec = up_radec()
        parity(tc, "goto_radec_j2000", {"ra_h": (ra + 12) % 24, "dec_deg": -dec}, r_below(15.0))
        assert fake.writes == []
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,125")
    with running() as (tc, fake, _):
        parity(tc, "goto_altaz", {"alt": 45.0, "az": 130.0}, R_SUN, "sun_target")
        assert fake.writes == []
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "45,150")
    with running() as (tc, fake, _):
        # T3.5c: a path through the disc is no longer refused, it is detoured (dry run only here)
        d = check(tc, "goto_altaz", {"alt": 45.0, "az": 180.0})
        assert d["ok"] is True and d["reason"] is None and d["code"] == "sun_detour", d
        assert d["detour"]["side"] in ("cw", "ccw") and d["detour"]["duration_s"] > 10.0
        assert fake.writes == []


def test_radec_sun_target(monkeypatch):
    ra, dec = up_radec()
    with running() as (tc, fake, app):
        site = app.state.poller.pwi4_site()
        alt, az = app.state.ephem.radec_j2000_altaz(site, ra, dec)
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", f"{alt},{az}")
    with running() as (tc, fake, _):
        d = parity(tc, "goto_radec_j2000", {"ra_h": ra, "dec_deg": dec}, R_SUN, "sun_target")
        assert d["target"]["alt"] == pytest.approx(alt, abs=0.05)


def test_no_az_representation():
    fake = FakePwi4()
    fake.set(mount__axis0__min_mech_position_degs=100, mount__axis0__max_mech_position_degs=140)
    fake.set(mount__axis0__position_degs=120)
    with running(fake) as (tc, fake, _):
        parity(tc, "goto_altaz", {"alt": 45.0, "az": 200.0}, R_NO_AZ, "no_az")
        assert fake.writes == []


def test_site_mismatch():
    fake = FakePwi4()
    with running(fake) as (tc, fake, app):
        fake.set(site__latitude_degs=20.0)
        wait_for(lambda: (app.state.poller.current_site_check() or object()).ok is False)
        parity(tc, "goto_altaz", ALTAZ, R_SITE, "site_mismatch")
        assert fake.writes == []


def test_not_homed_and_stalled():
    with running(homed=False) as (tc, fake, app):
        wait_for(lambda: app.state.control.homed.blocking)
        parity(tc, "goto_altaz", ALTAZ, R_NOT_HOMED, "not_homed")
        app.state.control.homed.stall(1)
        parity(tc, "goto_altaz", ALTAZ, R_STALLED, "stalled")
        assert fake.writes == []


def test_axis_disabled():
    with running() as (tc, fake, app):
        fake.set(mount__axis1__is_enabled=False)
        wait_for(lambda: not app.state.control.fresh_status().mount.axis1.is_enabled)
        parity(tc, "goto_altaz", ALTAZ, "Axis 1 disabled", "axis_disabled")
        assert fake.writes == []


def test_mount_not_connected():
    with running() as (tc, fake, app):
        fake.set(mount__is_connected=False)
        wait_for(lambda: not app.state.control.fresh_status().mount.is_connected)
        parity(tc, "goto_altaz", ALTAZ, R_NO_MOUNT, "no_mount")
        assert fake.writes == []


def test_pwi4_unreachable():
    with running() as (tc, fake, app):
        fake.down = True
        wait_for(lambda: app.state.control.fresh_status() is None)
        d = check(tc, "goto_altaz", ALTAZ)
        assert d["reason"] == R_NO_PWI4 and d["code"] == "no_pwi4" and d["ok"] is False
        assert d["target"] == {"alt": 45.0, "az": 125.0}
        w = Win(tc)
        assert w.send("goto_altaz", ALTAZ, "real")["reason"] == R_NO_PWI4
        w.close()


def test_sequence_busy():
    with running() as (tc, fake, app):
        app.state.control.seq._task = types.SimpleNamespace(done=lambda: False)
        try:
            d = check(tc, "goto_altaz", ALTAZ)
            assert d["reason"] == "Busy: homing" and d["code"] == "sequence_busy"
            w = Win(tc)
            assert w.send("goto_altaz", ALTAZ, "real")["reason"] == "Busy: homing"
            w.close()
        finally:
            app.state.control.seq._task = None


def test_starting_and_shutting_down():
    with running() as (tc, fake, app):
        ctl = app.state.control
        ctl.ready = False
        d = check(tc, "goto_altaz", ALTAZ)
        assert d["reason"] == R_STARTING and d["code"] == "starting"
        ctl.ready = True
        ctl.shutting_down = True
        d = check(tc, "goto_altaz", ALTAZ)
        assert d["reason"] == R_SHUTTING and d["code"] == "shutting_down"
        ctl.shutting_down = False


# ------------------------------------------------------------------ lock / audit / read-only


def test_lock_holder_is_not_evaluated():
    with running() as (tc, fake, app):
        w = Win(tc)
        assert w.send("take_control", cid="t")["ok"]
        holder = app.state.control.holder
        d = check(tc, "goto_altaz", ALTAZ)  # REST caller is not the holder: still ok
        assert d["ok"] is True
        w2 = Win(tc)
        assert w2.send("goto_altaz", ALTAZ, "real")["reason"] == R_OTHER_HAS_CONTROL
        assert app.state.control.holder == holder
        w2.close()
        w.close()


def test_check_takes_no_lock_writes_nothing_and_audits_nothing(tmp_path):
    with running() as (tc, fake, app):
        before = len(audit_lines(tmp_path))
        for args in ({"alt": 45.0, "az": 125.0}, {"alt": 5.0, "az": 125.0}):  # ok + refused
            check(tc, "goto_altaz", args)
        ra, dec = up_radec()
        check(tc, "goto_radec_j2000", {"ra_h": ra, "dec_deg": dec})
        check(tc, "goto_radec_j2000", {"ra_h": (ra + 12) % 24, "dec_deg": -dec})
        assert app.state.control.holder is None
        assert fake.writes == []
        assert app.state.safety._plans == {} and app.state.safety._target_az == {}
        assert len(audit_lines(tmp_path)) == before
        ev = [e["event"] for e in audit_lines(tmp_path)]
        assert "safety_refusal" not in ev and "cmd" not in ev
        # the real path still audits its refusal
        w = Win(tc)
        w.send("goto_altaz", {"alt": 5.0, "az": 125.0}, "real")
        w.close()
        ev = audit_lines(tmp_path)
        assert any(e["event"] == "safety_refusal" for e in ev)
        assert any(e["event"] == "cmd" and e["result"] == "refused" for e in ev)
