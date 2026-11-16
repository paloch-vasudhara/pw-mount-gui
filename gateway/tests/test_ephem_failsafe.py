"""T6.1a: ephemeris fail-safe. Without de421.bsp (and no test Sun override) there is no Sun
keep-out, so every motion command and HOME is refused with R_NO_EPHEM; stop / tracking off /
connect / disconnect stay available. The file appearing is picked up by the poller's retry.
Fakes only (FakePwi4); the real de421.bsp is only COPIED into tmp_path, never moved.
"""

from __future__ import annotations

import json
import shutil
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import REPO_ROOT, Settings
from gateway.control import R_NO_EPHEM
from gateway.ephem import Ephem

from .conftest import FakePwi4
from .test_check import ALTAZ, H, check
from .test_control import TOKEN, Win, audit_lines, wait_for
from .test_sequences import disconnected_fake, paths, seq_info, virtual, wait_seq

REAL_BSP = REPO_ROOT / "data" / "ephemeris" / "de421.bsp"
REASON = "Sun position unavailable (ephemeris missing)"

# every command of MOTION_COMMANDS (tracking ON), with valid arguments
MOTION: list[tuple[str, dict]] = [
    ("goto_altaz", ALTAZ),
    ("goto_radec_j2000", {"ra_h": 3.0, "dec_deg": 20.0}),
    ("tracking", {"on": True}),
    ("jog", {"axis": 0, "dir": 1, "speed": 1.0}),
    ("step", {"axis": 1, "dir": 1, "size_arcmin": 5}),
    ("wrap_turn", {}),
    ("track_object", {"norad": 25544}),
    ("track_aircraft", {"icao": "ABC123"}),
]
CHECKABLE = [m for m in MOTION if m[0] != "tracking"]  # /api/mount/check has no tracking


@contextmanager
def running_missing(
    tmp_path, monkeypatch, fake: FakePwi4 | None = None, sun: str = "", homed: bool = True
):
    """A gateway whose ephemeris file does not exist (`sun` = PW_TEST_SUN_ALTAZ, "" = none)."""
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", sun)
    fake = fake or FakePwi4()
    eph = tmp_path / "ephemeris" / "de421.bsp"  # not there
    app = create_app(Settings(port=8400, token=TOKEN, ephemeris=eph), transport=fake.transport)
    with TestClient(app, base_url="http://127.0.0.1:8400") as tc:
        wait_for(lambda: tc.get("/api/mount/status").status_code == 200)
        wait_for(lambda: app.state.control.ready)
        wait_for(lambda: app.state.control.fresh_status() is not None)
        if homed and "mount.is_connected=true" in fake.text:
            wait_for(lambda: app.state.control.homed.blocking)
            app.state.control.homed.override("test-fixture")
        yield tc, fake, app, eph


def events(tmp_path, name: str) -> list[dict]:
    return [e for e in audit_lines(tmp_path) if e["event"] == name]


# ------------------------------------------------------------------ Ephem.reload


def test_ephem_reload_loads_a_file_that_appears(tmp_path):
    p = tmp_path / "de421.bsp"
    e = Ephem(p)
    assert not e.available and e.error and e.timescale is None
    assert e.reload() is False and not e.available
    shutil.copy(REAL_BSP, p)
    assert e.reload() is True and e.available and e.error is None and e.timescale is not None
    assert e.sun_altaz((14.0, 100.0, 10.0))  # usable
    assert e.reload() is True  # a loaded ephemeris is kept


# ------------------------------------------------------------------ refusals (real commands)


def test_every_motion_command_is_refused_without_the_sun(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, fake, app, _):
        w = Win(tc)
        before = len(fake.writes)
        for i, (name, args) in enumerate(MOTION):
            ack = w.send(name, args, f"m{i}")
            assert ack["ok"] is False and ack["reason"] == R_NO_EPHEM == REASON, (name, ack)
        assert len(fake.writes) == before  # nothing reached PWI4
        assert app.state.control.holder is None  # a refused command does not take the lock
        w.close()
    refused = [e for e in events(tmp_path, "cmd") if e["result"] == "refused"]
    assert {e["cmd"] for e in refused} >= {m[0] for m in MOTION}


def test_dry_run_check_refuses_with_the_code(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, fake, app, _):
        for name, args in CHECKABLE:
            body = check(tc, name, args)
            assert body["ok"] is False and body["reason"] == REASON, (name, body)
            assert body["code"] == "no_ephemeris", (name, body)
        assert fake.writes == [] or all(p == "/mount/stop" for p, _ in fake.writes)
        assert not events(tmp_path, "safety_refusal")  # a dry run leaves no trace


def test_home_is_refused_without_the_sun(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, fake, app, _):
        w = Win(tc)
        ack = w.send("home")
        assert ack["ok"] is False and ack["reason"] == REASON
        assert "/mount/find_home" not in paths(fake)
        assert seq_info(app) is None
        w.close()


# ------------------------------------------------------------------ allowed commands


def test_stop_tracking_off_and_locks_stay_available(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, fake, app, _):
        w = Win(tc)
        assert w.send("take_control", None, "a1")["ok"] is True
        assert w.send("stop", None, "a2")["ok"] is True
        ack = w.send("tracking", {"on": False}, "a3")
        assert ack["ok"] is True, ack
        assert "/mount/stop" in paths(fake)
        assert w.send("jog_stop", None, "a4")["ok"] is True
        assert w.send("wrap_turn_cancel", None, "a5")["ok"] is True
        ack = w.send("homed_override", None, "a6")  # not in the not-homed state: a different reason
        assert ack["reason"] != REASON
        assert w.send("release_control", None, "a7")["ok"] is True
        w.close()


def test_connect_skips_the_home_with_the_reason(tmp_path, monkeypatch):
    fake = disconnected_fake()
    with running_missing(tmp_path, monkeypatch, fake) as (tc, _, app, _eph):
        virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"] is True
        info = wait_seq(app)
        assert info.status == "done" and info.error is None
        assert info.warning == f"Home skipped: {REASON}"
        p = paths(fake)
        assert "/mount/find_home" not in p and p.count("/mount/enable") == 2
        w.close()
    ev = events(tmp_path, "home_skipped")
    assert ev and ev[0]["reason"] == REASON and ev[0]["sun"] is False


def test_disconnect_asks_to_go_on_without_homing(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, fake, app, _):
        virtual(app)
        w = Win(tc)
        ack = w.send("disconnect", None, "d1")
        assert ack["ok"] is False and ack["code"] == "home_blocked_confirm"
        assert ack["reason"] == f"Home is blocked ({REASON}); disconnect without homing?"
        assert seq_info(app) is None
        ack = w.send("disconnect", {"skip_home": True}, "d2")
        assert ack["ok"] is True
        info = wait_seq(app)
        assert info.status == "done" and "without homing" in info.warning
        p = paths(fake)
        assert "/mount/find_home" not in p and p[-1] == "/mount/disconnect"
        w.close()
    assert events(tmp_path, "disconnect_without_home")


# ------------------------------------------------------------------ the test Sun override


def test_sun_override_counts_as_known(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch, sun="-60,0") as (tc, fake, app, _):
        assert app.state.safety.sun_known is True
        assert app.state.poller.latest.sun_known is True
        assert check(tc, "goto_altaz", ALTAZ)["ok"] is True
        assert check(tc, "jog", {"axis": 0, "dir": 1, "speed": 1.0})["ok"] is True
        w = Win(tc)
        assert w.send("goto_altaz", ALTAZ)["ok"] is True
        w.close()
        assert not events(tmp_path, "ephemeris_missing")  # the Sun is known: nothing to report
        # RA/Dec still cannot be converted without the file: its own refusal, same wording
        assert check(tc, "goto_radec_j2000", {"ra_h": 3.0, "dec_deg": 20.0})["reason"] == REASON


# ------------------------------------------------------------------ snapshot flag


def test_snapshot_flag(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, _, app, _):
        wait_for(lambda: app.state.poller.latest is not None)
        assert app.state.poller.latest.sun_known is False
        body = tc.get("/api/mount/status", headers=H).json()
        assert body["sun_known"] is False
        w = Win(tc)
        for _ in range(100):
            msg = json.loads(w.ws.receive_text())
            if msg["type"] == "status":
                assert msg["data"]["sun_known"] is False
                break
        else:
            raise AssertionError("no status")
        w.close()


def test_snapshot_flag_true_with_the_real_ephemeris(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "")  # no override: the real file decides
    fake = FakePwi4()
    app = create_app(Settings(port=8400, token=TOKEN), transport=fake.transport)
    with TestClient(app, base_url="http://127.0.0.1:8400"):
        wait_for(lambda: app.state.poller.latest is not None)
        assert app.state.safety.sun_known is True
        assert app.state.poller.latest.sun_known is True
    assert not events(tmp_path, "ephemeris_missing")


# ------------------------------------------------------------------ automatic reload


def test_the_file_appearing_is_reloaded_within_one_retry(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, fake, app, eph_path):
        poller, ephem = app.state.poller, app.state.ephem
        clock = [time.monotonic()]
        poller.ephem_clock = lambda: clock[0]
        poller._ephem_next = None  # re-arm with the fake clock
        poller.ephem_retry_s = 30.0
        calls = []
        real_reload = ephem.reload
        ephem.reload = lambda: calls.append(1) or real_reload()
        wait_for(lambda: poller._ephem_next is not None)  # armed: first try is 30 s away
        clock[0] += 29.0
        time.sleep(0.4)
        assert calls == [] and not ephem.available  # not before the interval
        clock[0] += 2.0  # 31 s: the retry runs, the file is still missing
        wait_for(lambda: len(calls) == 1)
        time.sleep(0.4)
        assert len(calls) == 1 and not ephem.available  # and not again until 30 s later
        assert app.state.poller.latest.sun_known is False
        w = Win(tc)
        assert w.send("goto_altaz", ALTAZ, "r1")["reason"] == REASON
        # the file appears
        eph_path.parent.mkdir(parents=True)
        shutil.copy(REAL_BSP, eph_path)
        clock[0] += 31.0
        wait_for(lambda: ephem.available, timeout=10.0)
        wait_for(lambda: app.state.poller.latest.sun_known is True)
        assert app.state.safety.sun_known is True
        # Sun / Moon appear (the site refresh follows the reload)
        wait_for(lambda: app.state.poller.latest.sun.alt is not None)
        # motion is allowed again (the Sun is real now: pick a target away from it)
        ack = w.send("tracking", {"on": False}, "r2")
        assert ack["ok"] is True
        assert check(tc, "goto_altaz", ALTAZ)["code"] != "no_ephemeris"
        n = len(calls)
        clock[0] += 120.0
        time.sleep(0.4)
        assert len(calls) == n  # no retries once loaded
        w.close()
    assert len(events(tmp_path, "ephemeris_missing")) == 1
    assert len(events(tmp_path, "ephemeris_loaded")) == 1


def test_missing_is_audited_once_at_start_not_every_retry(tmp_path, monkeypatch):
    with running_missing(tmp_path, monkeypatch) as (tc, fake, app, _):
        poller = app.state.poller
        clock = [time.monotonic()]
        poller.ephem_clock = lambda: clock[0]
        poller._ephem_next = None
        calls = []
        real = app.state.ephem.reload
        app.state.ephem.reload = lambda: calls.append(1) or real()
        wait_for(lambda: poller._ephem_next is not None)  # armed with the fake clock
        for _ in range(3):
            n = len(calls)
            clock[0] += 31.0
            wait_for(lambda n=n: len(calls) > n, timeout=5.0)
    ev = events(tmp_path, "ephemeris_missing")
    assert len(ev) == 1 and ev[0]["error"]
    assert not events(tmp_path, "ephemeris_loaded")


@pytest.mark.parametrize("name,args", MOTION)
def test_with_the_ephemeris_loaded_nothing_new_is_refused(name, args, monkeypatch):
    """The fail-safe never refuses on its own while the Sun is known (real de421.bsp)."""
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")
    fake = FakePwi4()
    app = create_app(Settings(port=8400, token=TOKEN), transport=fake.transport)
    with TestClient(app, base_url="http://127.0.0.1:8400"):
        wait_for(lambda: app.state.control.fresh_status() is not None)
        assert app.state.control._gateway_refusal(_cmd(name, args), None) != R_NO_EPHEM


def _cmd(name, args):
    from gateway.commands import Command

    return Command("x", name, args)
