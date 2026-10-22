"""T3.1a: command protocol, control lock, STOP preemption, link-loss / start-up / shutdown stop,
audit. All against FakePwi4 (recorded requests); no real PWI4 is contacted.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.audit import AuditLog
from gateway.commands import MAX_MESSAGE_BYTES, parse_command
from gateway.config import Settings
from gateway.control import nearest_wrap_min

from .conftest import FakePwi4

TOKEN = "t" * 43
HDR = {"origin": "http://127.0.0.1:3200", "host": "127.0.0.1:8400"}


def up_radec() -> tuple[float, float]:
    """A J2000 RA/Dec ~70 deg up at the fake PWI4 site right now (meridian, lat - 20)."""
    from gateway.snapshot import jd_from_utc, lmst_hours_from

    jd = jd_from_utc(datetime.now(UTC))
    return round(lmst_hours_from(jd, 100.5656) % 24.0, 3), 14.3479 - 20.0


def cmd(name: str, args: dict | None = None, cid: str = "c1") -> str:
    return json.dumps({"type": "cmd", "id": cid, "cmd": name, "args": args or {}})


class Win:
    """One browser window: an authenticated WS plus ack / status helpers."""

    def __init__(self, tc: TestClient) -> None:
        self.cm = tc.websocket_connect("/api/mount/ws", headers=HDR)
        self.ws = self.cm.__enter__()
        self.ws.send_text(json.dumps({"type": "auth", "token": TOKEN}))
        assert json.loads(self.ws.receive_text())["type"] == "hello"

    def send(self, name: str, args: dict | None = None, cid: str = "c1") -> dict:
        self.ws.send_text(cmd(name, args, cid))
        return self.wait_ack(cid)

    def wait_ack(self, cid: str | None = "c1") -> dict:
        for _ in range(400):
            msg = json.loads(self.ws.receive_text())
            if msg["type"] == "ack" and (cid is None or msg["id"] == cid):
                return msg
        raise AssertionError("no ack")

    def expect_control(self, want: str) -> None:
        """Read statuses (skipping stale buffered ones) until the holder reads `want`."""
        seen = []
        for _ in range(300):
            msg = json.loads(self.ws.receive_text())
            if msg["type"] == "status":
                seen.append(msg["data"]["control"]["holder"])
                if seen[-1] == want:
                    return
        raise AssertionError(f"never saw holder {want}: {seen[-5:]}")

    def close(self) -> None:
        # Let the server-side handler finish before TestClient cancels its task scope
        # (otherwise a slow teardown surfaces as a spurious CancelledError).
        self.ws.close(1000)
        time.sleep(0.2)
        self.cm.__exit__(None, None, None)


def wait_for(pred, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


def audit_lines(tmp_path) -> list[dict]:
    out: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


@contextmanager
def running(fake: FakePwi4 | None = None, wait_ready: bool = True):
    fake = fake or FakePwi4()
    app = create_app(Settings(port=8400, token=TOKEN), transport=fake.transport)
    with TestClient(app, base_url="http://127.0.0.1:8400") as tc:
        if wait_ready:
            wait_for(lambda: tc.get("/api/mount/status").status_code == 200)
            wait_for(lambda: app.state.control.ready)
            wait_for(lambda: app.state.control.fresh_status() is not None)
        yield tc, fake, app


# ------------------------------------------------------------------ protocol


def test_parse_ok_and_int_accepted_for_float():
    c = parse_command(cmd("goto_altaz", {"alt": 45, "az": 120.5}))
    assert c.name == "goto_altaz" and c.args == {"alt": 45.0, "az": 120.5}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"type": "cmd", "id": "x"}),  # no cmd
        json.dumps({"type": "cmd", "id": "", "cmd": "stop"}),
        json.dumps({"type": "cmd", "id": "x" * 65, "cmd": "stop"}),
        json.dumps({"type": "cmd", "id": 5, "cmd": "stop"}),
        json.dumps({"type": "cmd", "id": "x", "cmd": "reboot"}),
        json.dumps({"type": "cmd", "id": "x", "cmd": "stop", "extra": 1}),
        json.dumps({"type": "cmd", "id": "x", "cmd": "stop", "args": {"a": 1}}),
        cmd("goto_altaz", {"alt": 45}),
        cmd("goto_altaz", {"alt": "45", "az": 10}),
        cmd("goto_altaz", {"alt": True, "az": 10}),
        cmd("goto_altaz", {"alt": 95, "az": 10}),
        cmd("goto_altaz", {"alt": -1, "az": 10}),
        cmd("goto_altaz", {"alt": 45, "az": 360}),
        cmd("goto_altaz", {"alt": 45, "az": 10, "x": 1}),
        '{"type":"cmd","id":"x","cmd":"goto_altaz","args":{"alt":NaN,"az":1}}',
        cmd("goto_radec_j2000", {"ra_h": 24, "dec_deg": 0}),
        cmd("goto_radec_j2000", {"ra_h": 1, "dec_deg": 91}),
        cmd("tracking", {"on": 1}),
        cmd("tracking", {}),
        "x" * (MAX_MESSAGE_BYTES + 1),
    ],
)
def test_invalid_commands_refused_without_pwi4_write(raw):
    with running() as (tc, fake, _):
        w = Win(tc)
        w.ws.send_text(raw)
        ack = w.wait_ack(None)
        assert ack["ok"] is False and ack["reason"]
        assert fake.writes == []
        w.close()


# ------------------------------------------------------------------ lock


def test_first_command_takes_control_and_other_is_refused(tmp_path):
    with running() as (tc, fake, _):
        a, b = Win(tc), Win(tc)
        assert a.send("goto_altaz", {"alt": 45, "az": 125})["ok"]
        a.expect_control("you")
        b.expect_control("other")
        ack = b.send("goto_altaz", {"alt": 50, "az": 125}, "c2")
        assert not ack["ok"] and ack["reason"] == "Another window has control"
        assert ack["control"]["holder"] == "other"
        gotos = [c for c in fake.writes if c[0] == "/mount/goto_alt_az"]
        assert len(gotos) == 1
        a.close()
        b.close()
    events = [e["event"] for e in audit_lines(tmp_path)]
    assert "lock_taken" in events


def test_stop_from_non_holder_always_accepted():
    with running() as (tc, fake, _):
        a, b = Win(tc), Win(tc)
        assert a.send("take_control")["ok"]
        ack = b.send("stop", cid="s1")
        assert ack["ok"]
        assert ("/mount/stop", {}) in fake.writes
        assert b.send("tracking", {"on": True}, "c3")["reason"] == "Another window has control"
        a.close()
        b.close()


def test_takeover_and_release_do_not_stop_anything():
    with running() as (tc, fake, _):
        a, b = Win(tc), Win(tc)
        assert a.send("take_control")["ok"]
        assert b.send("take_control")["ok"]
        b.expect_control("you")
        a.expect_control("other")
        assert not a.send("release_control", cid="r1")["ok"]  # not the holder
        assert b.send("release_control", cid="r2")["ok"]
        a.expect_control("none")
        assert fake.writes == []
        a.close()
        b.close()


def test_viewer_disconnect_stops_nothing():
    with running() as (tc, fake, _):
        a, b = Win(tc), Win(tc)
        assert a.send("goto_altaz", {"alt": 45, "az": 125})["ok"]
        n = len(fake.writes)
        b.close()
        time.sleep(0.5)
        assert len(fake.writes) == n
        a.close()


# ------------------------------------------------------------------ writes


def test_goto_altaz_sets_wrap_then_goto():
    with running() as (tc, fake, _):
        a = Win(tc)
        assert a.send("goto_altaz", {"alt": 40, "az": 300})["ok"]
        paths = [p for p, _ in fake.writes]
        # axis0 at 120 deg: nearest representation of az 300 is -60 -> wrap window moves
        # P15: tracking off first (PWI4's goto_alt_az does not end tracking)
        assert paths == [
            "/mount/tracking_off",
            "/mount/set_axis0_wrap_range_min",
            "/mount/goto_alt_az",
        ]
        assert fake.writes[2][1] == {"alt_degs": "40.0", "az_degs": "300.0"}
        a.close()


def test_goto_radec_and_tracking_calls():
    with running() as (tc, fake, _):
        a = Win(tc)
        ra, dec = up_radec()
        assert a.send("goto_radec_j2000", {"ra_h": ra, "dec_deg": dec}, "g")["ok"]
        assert a.send("tracking", {"on": True}, "t1")["ok"]
        assert a.send("tracking", {"on": False}, "t0")["ok"]
        got = [(p, q) for p, q in fake.writes if not p.endswith("wrap_range_min")]
        assert got == [
            ("/mount/goto_ra_dec_j2000", {"ra_hours": str(ra), "dec_degs": str(dec)}),
            ("/mount/tracking_on", {}),
            ("/mount/tracking_off", {}),
        ]
        a.close()


def test_only_whitelisted_write_endpoints_ever_used():
    allowed = {
        "/mount/stop",
        "/mount/tracking_on",
        "/mount/tracking_off",
        "/mount/goto_alt_az",
        "/mount/goto_ra_dec_j2000",
        "/mount/set_axis0_wrap_range_min",
    }
    with running() as (tc, fake, _):
        a = Win(tc)
        a.send("goto_altaz", {"alt": 40, "az": 10}, "1")
        ra, dec = up_radec()
        a.send("goto_radec_j2000", {"ra_h": ra, "dec_deg": dec}, "2")
        a.send("tracking", {"on": True}, "3")
        a.send("stop", cid="4")
        a.close()
        assert {p for p, _ in fake.writes} <= allowed


def test_mount_not_connected_refused():
    fake = FakePwi4()
    fake.set(mount__is_connected=False)
    with running(fake) as (tc, _, _):
        a = Win(tc)
        ack = a.send("goto_altaz", {"alt": 40, "az": 10})
        assert ack["reason"] == "Mount not connected"
        assert fake.writes == []
        a.close()


def test_axis_disabled_refused():
    fake = FakePwi4()
    fake.set(mount__axis1__is_enabled=False)
    with running(fake) as (tc, _, _):
        a = Win(tc)
        assert a.send("tracking", {"on": True})["reason"] == "Axis 1 disabled"
        assert fake.writes == []
        a.close()


def test_check_command_seam_can_refuse():
    with running() as (tc, fake, app):
        app.state.control.check = lambda c, ctx: "Below station limit elevation (test)"
        a = Win(tc)
        ack = a.send("goto_altaz", {"alt": 40, "az": 10})
        assert ack["reason"] == "Below station limit elevation (test)"
        assert fake.writes == []
        assert a.send("stop", cid="s")["ok"]  # STOP is never subject to the hook
        a.close()


def test_pwi4_http_error_is_reported_in_ack():
    fake = FakePwi4()
    fake.fail_writes = {"/mount/goto_alt_az"}
    with running(fake) as (tc, _, _):
        a = Win(tc)
        ack = a.send("goto_altaz", {"alt": 40, "az": 125})
        assert not ack["ok"] and "HTTP 500" in ack["reason"]
        a.close()


# ------------------------------------------------------------------ STOP preemption


def test_stop_preempts_inflight_and_queued_commands():
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        a, b = Win(tc), Win(tc)
        assert a.send("take_control")["ok"]
        fake.write_delay = 0.6  # PWI4 hangs on the goto
        a.ws.send_text(cmd("goto_altaz", {"alt": 40, "az": 125}, "g1"))
        a.ws.send_text(cmd("goto_altaz", {"alt": 41, "az": 126}, "g2"))
        time.sleep(0.15)
        fake.write_delay = 0.0
        t0 = time.monotonic()
        ack = b.send("stop", cid="s")
        assert ack["ok"] and time.monotonic() - t0 < 0.4
        acks = {}
        for _ in range(2):
            m = a.wait_ack(None)
            acks[m["id"]] = m
        assert acks["g1"]["ok"] is False and acks["g1"]["reason"] == "Cancelled by STOP"
        assert acks["g2"]["ok"] is False and acks["g2"]["reason"] == "Cancelled by STOP"
        time.sleep(0.8)
        assert len([c for c in fake.writes if c[0] == "/mount/goto_alt_az"]) <= 1
        a.close()
        b.close()


def test_stop_ends_tracking_via_confirm_when_mount_stop_does_not():
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        a = Win(tc)
        fake.set(mount__is_tracking=True)
        time.sleep(0.3)
        writes_before = len(fake.writes)
        assert a.send("stop", cid="s")["ok"]
        wait_for(lambda: len(fake.writes) >= writes_before + 2)
        assert [p for p, _ in fake.writes][writes_before : writes_before + 2] == [
            "/mount/stop",
            "/mount/tracking_off",
        ]
        a.close()


def test_no_tracking_off_when_mount_stop_ends_tracking():
    fake = FakePwi4()
    fake.stop_ends_tracking = True
    with running(fake) as (tc, _, _):
        a = Win(tc)
        fake.set(mount__is_tracking=True)
        time.sleep(0.3)
        assert a.send("stop", cid="s")["ok"]
        time.sleep(0.5)
        assert [p for p, _ in fake.writes] == ["/mount/stop"]
        a.close()


def test_stop_delivery_failure_is_reported():
    fake = FakePwi4()
    fake.fail_writes = {"/mount/stop"}
    with running(fake) as (tc, _, _):
        a = Win(tc)
        ack = a.send("stop", cid="s")
        assert not ack["ok"] and ack["reason"].startswith("STOP not delivered")
        assert len([c for c in fake.writes if c[0] == "/mount/stop"]) == 2  # one retry
        a.close()


# ------------------------------------------------------------------ link loss


def test_holder_lost_while_slewing_stops_audits_and_releases(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, app):
        a, b = Win(tc), Win(tc)
        assert a.send("goto_altaz", {"alt": 40, "az": 125})["ok"]
        a.close()
        wait_for(lambda: ("/mount/stop", {}) in fake.writes)
        wait_for(lambda: app.state.control.holder is None)
        b.expect_control("none")
        n = len(fake.writes)
        time.sleep(0.5)
        gotos = [c for c in fake.writes if c[0].startswith("/mount/goto")]
        assert len(gotos) == 1  # nothing resumes
        assert len(fake.writes) == n
        c = Win(tc)  # reconnect: still stopped, no lock
        c.expect_control("none")
        b.close()
        c.close()
    lines = audit_lines(tmp_path)
    ev = [e for e in lines if e["event"] == "link_loss_stop"]
    assert len(ev) == 1 and ev[0]["motion_active"] is True and ev[0]["confirmed"] is True
    assert any(e["event"] == "lock_released" and e["how"] == "link_loss" for e in lines)


def test_holder_lost_while_tracking_ends_tracking():
    fake = FakePwi4()
    fake.set(mount__is_tracking=True)
    with running(fake) as (tc, _, app):
        a = Win(tc)
        assert a.send("take_control")["ok"]
        n = len(fake.writes)
        fake.set(mount__is_tracking=True)  # start-up already ended it; track again
        a.close()
        wait_for(lambda: len(fake.writes) > n)
        wait_for(lambda: app.state.control.holder is None)
        assert [p for p, _ in fake.writes][n:][:2] == ["/mount/stop", "/mount/tracking_off"]


def test_holder_lost_while_idle_sends_no_stop(tmp_path):
    with running() as (tc, fake, app):
        a = Win(tc)
        assert a.send("take_control")["ok"]
        a.close()
        wait_for(lambda: app.state.control.holder is None)
        assert fake.writes == []
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "link_loss_stop"]
    assert ev and ev[0]["motion_active"] is False


def test_holder_lost_right_after_goto_stops_even_if_status_shows_no_motion():
    fake = FakePwi4()
    with running(fake) as (tc, _, _):
        a = Win(tc)
        assert a.send("goto_altaz", {"alt": 40, "az": 125})["ok"]
        fake.set(mount__is_slewing=False)  # status has not caught up with the goto
        a.close()
        wait_for(lambda: ("/mount/stop", {}) in fake.writes)


# ------------------------------------------------------------------ start-up / shutdown


def test_startup_stop_when_found_moving(tmp_path):
    fake = FakePwi4()
    fake.set(mount__is_slewing=True, mount__is_tracking=True)
    with running(fake) as (_, _, app):
        assert [p for p, _ in fake.writes][:2] == ["/mount/stop", "/mount/tracking_off"]
        assert app.state.control.ready
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "startup_stop"]
    assert ev and ev[0]["was_slewing"] and ev[0]["was_tracking"] and ev[0]["confirmed"]


def test_startup_idle_makes_no_writes(tmp_path):
    with running() as (_, fake, _):
        assert fake.writes == []
    assert any(e["event"] == "startup" for e in audit_lines(tmp_path))


def test_commands_refused_while_pwi4_unreachable_at_start():
    fake = FakePwi4()
    fake.down = True
    with running(fake, wait_ready=False) as (tc, _, app):
        assert not app.state.control.ready
        w = Win(tc)
        ack = w.send("goto_altaz", {"alt": 40, "az": 125})
        assert not ack["ok"] and ack["reason"] == "PWI4 not running / not reachable"
        assert w.send("stop", cid="s")["ok"] is False  # STOP is attempted; PWI4 is down
        w.close()
        assert {p for p, _ in fake.writes} == {"/mount/stop"}  # only STOP attempts (all refused)


def test_shutdown_stop(tmp_path):
    fake = FakePwi4()
    with running(fake):
        fake.set(mount__is_slewing=True)
    assert [p for p, _ in fake.writes][:1] == ["/mount/stop"]
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "shutdown_stop"]
    assert ev and ev[0]["motion_active"] is True


def test_shutdown_idle_makes_no_writes():
    with running() as (_, fake, _):
        pass
    assert fake.writes == []


# ------------------------------------------------------------------ audit + helpers


def test_audit_entries_have_expected_fields_and_no_secrets(tmp_path):
    with running() as (tc, _, _):
        a = Win(tc)
        a.send("goto_altaz", {"alt": 40, "az": 125})
        a.send("stop", cid="s")
        a.ws.send_text("garbage")
        a.wait_ack(None)
        a.close()
    lines = audit_lines(tmp_path)
    cmds = [e for e in lines if e["event"] == "cmd"]
    goto = next(e for e in cmds if e["cmd"] == "goto_altaz")
    assert goto["result"] == "ok" and goto["args"] == {"alt": 40.0, "az": 125.0}
    assert goto["client"] and datetime.fromisoformat(goto["ts"]).tzinfo is not None
    assert any(e["result"] == "refused" and e["reason"] for e in cmds)
    assert TOKEN not in json.dumps(lines)
    assert (tmp_path / "audit" / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").exists()


def test_audit_cleanup_90_days(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    today = date(2026, 10, 21)
    old = today - timedelta(days=91)
    keep = today - timedelta(days=90)
    for day in (old, keep, today):
        (d / f"{day.isoformat()}.jsonl").write_text("{}\n")
    (d / "notes.txt").write_text("x")
    removed = AuditLog(d).cleanup(today)
    assert removed == [f"{old.isoformat()}.jsonl"]
    assert sorted(p.name for p in d.iterdir()) == sorted(
        ["notes.txt", f"{keep.isoformat()}.jsonl", f"{today.isoformat()}.jsonl"]
    )


def test_audit_never_raises_and_drops_secret_keys(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    log = AuditLog(f / "sub")  # parent is a file -> mkdir fails
    log.write("x", a=1)
    assert log.errors == 1
    ok = AuditLog(tmp_path / "ok")
    e = ok.write("x", token="abc", args={"api_key": "k", "alt": 1})
    assert "token" not in e and e["args"] == {"alt": 1}


def test_nearest_wrap_min():
    assert nearest_wrap_min(120.0, 100.0, -300.0, 300.0) == 100.0 - 180.0
    assert nearest_wrap_min(-250.0, 100.0, -300.0, 300.0) == -260.0 - 180.0  # -260 is nearest
    assert nearest_wrap_min(0.0, 10.0, 0.0, 100.0) == 10.0 - 180.0
    assert nearest_wrap_min(0.0, 200.0, 0.0, 100.0) is None  # no representation in range


def test_concurrent_commands_are_serialized():
    fake = FakePwi4()
    seen = {"now": 0, "max": 0}
    orig = fake.handler

    async def handler(request):
        if not request.url.path.startswith("/mount/"):
            return await orig(request)
        seen["now"] += 1
        seen["max"] = max(seen["max"], seen["now"])
        try:
            return await orig(request)
        finally:
            seen["now"] -= 1

    fake.handler = handler  # type: ignore[method-assign]
    with running(fake) as (tc, _, _):
        a = Win(tc)
        fake.write_delay = 0.1
        for i in range(3):
            a.ws.send_text(cmd("tracking", {"on": True}, f"t{i}"))
        for _ in range(3):
            assert a.wait_ack(None)["ok"]
        assert seen["max"] == 1
        a.close()
