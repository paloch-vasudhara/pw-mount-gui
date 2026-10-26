"""T3.2: connection sequences (A5a), HOME, the home Sun check, BUSY/HOMING state, failure
injection at every step, STOP mid-sequence. All against FakePwi4 (no real PWI4 is contacted).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from gateway import state
from gateway.commands import CommandError, parse_command
from gateway.control import HomeVerdict, Pwi4Error
from gateway.ephem import Ephem
from gateway.pwi4_io import PollResult, parse
from gateway.safety import (
    HomingWatch,
    SafetyEngine,
    SafetyMonitor,
    actual_limit_decision,
)
from gateway.sequences import (
    HOME_TIMEOUT_S,
    Sequencer,
    StepError,
    find_pwi4_exe,
)
from gateway.settings import MemorySecretStore, SettingsStore, StationSettings

from .conftest import FakePwi4, status_text
from .test_control import Win, audit_lines, cmd, running, wait_for

SUN_ON_HOME_PATH = "65,150"  # the fake mount (az 120 / alt 45) homes to 85 / 180 through this


class VClock:
    """Virtual time for the sequencer: sleeping advances the clock instantly."""

    def __init__(self) -> None:
        self.t = 0.0
        self.on_sleep = None

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        if self.on_sleep:
            self.on_sleep(self.t)
        await asyncio.sleep(0)


def virtual(app) -> VClock:
    vc = VClock()
    app.state.control.seq.clock = vc.now
    app.state.control.seq.sleep = vc.sleep
    return vc


def disconnected_fake() -> FakePwi4:
    fake = FakePwi4()
    fake.set(
        mount__is_connected=False,
        mount__axis0__is_enabled=False,
        mount__axis1__is_enabled=False,
    )
    return fake


def match_site(app) -> None:
    """The store was seeded with the default site (PWI4 was down at start): align it with the
    fake PWI4's site so the site gate passes."""
    loc = app.state.store.current.location
    loc.lat_deg, loc.lon_deg, loc.alt_m = 14.3479, 100.5656, 12.0


def seq_info(app):
    return app.state.control.seq.info


def wait_seq(app, timeout: float = 20.0) -> object:
    wait_for(lambda: seq_info(app) is not None and seq_info(app).status != "running", timeout)
    wait_for(lambda: not app.state.control.seq.active, timeout)  # incl. the make-safe tail
    return seq_info(app)


def paths(fake: FakePwi4) -> list[str]:
    return [p for p, _ in fake.writes]


def collect_states(w: Win, app, timeout: float = 20.0) -> list[dict]:
    """Status messages (state, reason, sequence) until the sequence has ended."""
    out: list[dict] = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        msg = json.loads(w.ws.receive_text())
        if msg["type"] == "status":
            d = msg["data"]
            out.append({"state": d["state"], "reason": d["state_reason"], "seq": d["sequence"]})
            si = seq_info(app)
            if si is not None and si.status != "running" and d["sequence"] is not None:
                if d["sequence"]["status"] != "running":
                    return out
    raise AssertionError("sequence did not end")


def home_fast(fake: FakePwi4, app) -> VClock:
    """Slow every PWI4 write a little so BUSY is observable, run waits on virtual time."""
    fake.write_delay = 0.25  # every step is visible in the 10 Hz status stream
    return virtual(app)


# ------------------------------------------------------------------ schemas


def test_sequence_command_schemas():
    assert parse_command(cmd("connect")).name == "connect"
    assert parse_command(cmd("home")).args == {}
    assert parse_command(cmd("disconnect")).args == {"skip_home": False}
    assert parse_command(cmd("disconnect", {"skip_home": True})).args == {"skip_home": True}
    for bad in (
        cmd("connect", {"x": 1}),
        cmd("home", {"skip_home": True}),
        cmd("disconnect", {"skip_home": 1}),
        cmd("disconnect", {"skip_home": "yes"}),
        cmd("disconnect", {"skip_home": True, "x": 1}),
    ):
        with pytest.raises(CommandError):
            parse_command(bad)


# ------------------------------------------------------------------ state precedence


def test_busy_wins_over_disconnected_and_fault_homing_does_not():
    from gateway.state import compute_state

    base = dict(
        reachable=True,
        mount_connected=False,
        axis_enabled=(False, False),
        slewing_raw=False,
        tracking_raw=False,
        moving=False,
    )
    try:
        state.set_override("BUSY", "Connecting: connect mount…")
        assert compute_state(**base) == ("BUSY", "Connecting: connect mount…")
        assert compute_state(**{**base, "reachable": False})[0] == "BUSY"  # PWI4 still starting
        assert compute_state(**{**base, "mount_connected": True})[0] == "BUSY"  # axes disabled
        state.set_override("HOMING", "homing")
        assert compute_state(**base)[0] == "DISCONNECTED"
        assert compute_state(**{**base, "mount_connected": True})[0] == "FAULT"
    finally:
        state.clear_override()


# ------------------------------------------------------------------ connect: success


def test_connect_success_busy_steps_and_write_order(tmp_path):
    fake = disconnected_fake()
    launched: list[int] = []
    with running(fake) as (tc, _, app):
        app.state.control.seq.launcher = lambda: launched.append(1) or {}
        home_fast(fake, app)
        w = Win(tc)
        ack = w.send("connect")
        assert ack["ok"] and ack["reason"] is None
        states = collect_states(w, app)
        info = seq_info(app)
        assert info.status == "done" and info.error is None and info.warning is None
        assert info.steps == ["start PWI4", "connect mount", "enable axes", "home mount"]
        busy = [s for s in states if s["state"] == "BUSY"]
        assert busy, "BUSY never seen"
        reasons = {s["reason"] for s in busy}
        assert "Connecting: connect mount…" in reasons and "Connecting: enable axes…" in reasons
        assert any(s["seq"] and s["seq"]["step"] == "enable axes" for s in busy)
        # the sequence stays in the snapshot after it ended
        final = states[-1]
        assert final["seq"]["status"] == "done"
        w.close()
        p = paths(fake)
        assert p.index("/mount/connect") < p.index("/mount/enable") < p.index("/mount/find_home")
        assert p.count("/mount/enable") == 2 and "/mount/disable" not in p
        assert [q for c, q in fake.writes if c == "/mount/enable"] == [
            {"axis": "0"},
            {"axis": "1"},
        ]
        assert launched == []  # PWI4 was running: no launch
    assert state._override is None
    ev = [e["event"] for e in audit_lines(tmp_path)]
    assert ev.count("sequence_step") == 4
    assert "sequence_start" in ev and "sequence_done" in ev and "home_finished" in ev


def test_connect_is_idempotent_for_satisfied_steps():
    with running() as (tc, fake, app):  # mount already connected, axes enabled
        virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        assert wait_seq(app).status == "done"
        p = paths(fake)
        assert "/mount/connect" not in p and "/mount/enable" not in p
        assert "/mount/find_home" in p  # HOME still runs
        w.close()


def test_connect_enables_only_the_missing_axis():
    fake = FakePwi4()
    fake.set(mount__axis1__is_enabled=False)
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        assert wait_seq(app).status == "done"
        assert [q for c, q in fake.writes if c == "/mount/enable"] == [{"axis": "1"}]
        w.close()


# ------------------------------------------------------------------ disconnect: success


def test_disconnect_success_order_and_state(tmp_path):
    fake = FakePwi4()
    with running(fake) as (tc, _, app):
        home_fast(fake, app)
        w = Win(tc)
        assert w.send("disconnect")["ok"]
        states = collect_states(w, app)
        assert seq_info(app).status == "done"
        assert seq_info(app).steps == [
            "stop motion",
            "home mount",
            "disable axes",
            "disconnect mount",
        ]
        assert any(s["state"] == "BUSY" and "Disconnecting" in s["reason"] for s in states)
        p = paths(fake)
        assert p.index("/mount/stop") < p.index("/mount/find_home") < p.index("/mount/disable")
        assert p.index("/mount/disable") < p.index("/mount/disconnect")
        assert p.count("/mount/disable") == 2 and p[-1] == "/mount/disconnect"
        wait_for(lambda: app.state.poller.latest.state == "DISCONNECTED")
        w.close()
    assert state._override is None


# ------------------------------------------------------------------ standalone home


def test_home_no_motion_completes_and_shows_homing():
    fake = FakePwi4()
    with running(fake) as (tc, _, app):
        home_fast(fake, app)
        w = Win(tc)
        assert w.send("home")["ok"]
        states = collect_states(w, app)
        assert any(s["state"] == "HOMING" for s in states)
        assert seq_info(app).status == "done"
        assert paths(fake).count("/mount/find_home") == 1
        wait_for(lambda: app.state.poller.latest.state == "STOPPED")  # HOMING is released
        w.close()


def test_home_with_motion_waits_until_stationary(tmp_path):
    fake = FakePwi4()
    # starts moving 0.3 s after find_home, moves for 0.8 s, then stands still
    script: list[tuple[float, dict]] = [(0.3, {})]
    for i in range(1, 9):
        a0, a1 = 120 + 7.5 * i, 45 + 5 * i
        script.append(
            (
                0.1,
                {
                    "mount__axis0__position_degs": a0,
                    "mount__azimuth_degs": a0,
                    "mount__axis1__position_degs": a1,
                    "mount__altitude_degs": a1,
                },
            )
        )
    fake.home_script = script
    with running(fake) as (tc, _, app):
        w = Win(tc)
        t0 = time.monotonic()
        assert w.send("home")["ok"]
        wait_seq(app, 30)
        dur = time.monotonic() - t0
        assert seq_info(app).status == "done"
        assert dur > 2.0, dur  # find_home + 0.3 + 0.8 motion + 1.0 s still
        w.close()
    fin = [e for e in audit_lines(tmp_path) if e["event"] == "home_finished"]
    assert fin and fin[0]["how"] == "completed"


def test_home_no_motion_within_3s_is_a_noop_pass(tmp_path):
    with running() as (tc, fake, app):
        vc = virtual(app)
        w = Win(tc)
        assert w.send("home")["ok"]
        assert wait_seq(app).status == "done"
        assert 3.0 <= vc.t < 5.0
        w.close()
    fin = [e for e in audit_lines(tmp_path) if e["event"] == "home_finished"]
    assert fin and fin[0]["how"] == "no_motion"


def test_home_times_out_after_180s():
    """Positions keep changing forever: failure at 180 s (virtual clock, stub writer)."""
    n = {"i": 0}

    async def read_status():
        n["i"] += 1
        return PollResult(
            True,
            status=parse(
                status_text(
                    {
                        "mount.axis0.position_degs": 10 + 0.5 * n["i"],
                        "mount.axis1.position_degs": 45,
                    }
                )
            ),
        )

    ctl = SimpleNamespace(writer=SimpleNamespace(read_status=read_status))
    seq = Sequencer(ctl)  # type: ignore[arg-type]
    vc = VClock()
    seq.clock, seq.sleep = vc.now, vc.sleep
    with pytest.raises(StepError, match="did not finish within 180 s"):
        asyncio.run(seq._wait_home_done())
    assert vc.t >= HOME_TIMEOUT_S


def test_home_wait_detects_motion_then_stationary():
    n = {"i": 0}

    async def read_status():
        n["i"] += 1
        i = n["i"]
        pos = 10 + 1.0 * min(i, 20)  # 10 deg/s for 2 s, then still
        return PollResult(
            True,
            status=parse(
                status_text({"mount.axis0.position_degs": pos, "mount.axis1.position_degs": 45})
            ),
        )

    ctl = SimpleNamespace(writer=SimpleNamespace(read_status=read_status))
    seq = Sequencer(ctl)  # type: ignore[arg-type]
    vc = VClock()
    seq.clock, seq.sleep = vc.now, vc.sleep
    assert asyncio.run(seq._wait_home_done()) == "completed"
    assert 3.0 <= vc.t < 3.6  # 2 s of motion + 1 s still (+ one poll)


# ------------------------------------------------------------------ PWI4 start


def test_connect_starts_pwi4_with_fake_launcher_unreachable_to_reachable(tmp_path):
    fake = disconnected_fake()
    fake.down = True
    calls: list[int] = []
    with running(fake, wait_ready=False) as (tc, _, app):
        ctl = app.state.control
        wait_for(lambda: ctl._startup_deferred, 5)
        assert not ctl.ready
        match_site(app)
        vc = virtual(app)

        def launcher():
            calls.append(1)
            return {"exe": "fake", "launched": True}

        ctl.seq.launcher = launcher
        # PWI4 "comes up" after 5 virtual seconds
        vc.on_sleep = lambda t: setattr(fake, "down", t < 5.0)
        w = Win(tc)
        ack = w.send("connect")
        assert ack["ok"], ack
        assert wait_seq(app, 30).status == "done"
        assert calls == [1]
        assert ctl.ready
        assert 4.5 <= vc.t < 12
        p = paths(fake)
        assert p.index("/mount/connect") < p.index("/mount/find_home")
        w.close()
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "pwi4_launch"]
    assert ev and ev[0]["exe"] == "fake"


def test_pwi4_start_timeout_60s():
    fake = disconnected_fake()
    fake.down = True
    with running(fake, wait_ready=False) as (tc, _, app):
        ctl = app.state.control
        wait_for(lambda: ctl._startup_deferred, 5)
        vc = virtual(app)
        ctl.seq.launcher = lambda: {"launched": True}
        w = Win(tc)
        assert w.send("connect")["ok"]
        info = wait_seq(app, 30)
        assert info.status == "failed"
        assert info.error == "Start PWI4 failed: PWI4 did not answer within 60 s"
        assert 60.0 <= vc.t < 62.0
        assert fake.writes == [] or {p for p in paths(fake)} <= {"/mount/stop"}
        assert state._override is None
        w.close()


def test_pwi4_exe_missing_reports_failure():
    fake = disconnected_fake()
    fake.down = True
    with running(fake, wait_ready=False) as (tc, _, app):
        ctl = app.state.control
        wait_for(lambda: ctl._startup_deferred, 5)

        def launcher():
            raise StepError("PWI4 executable not found (set PW_PWI4_EXE)")

        ctl.seq.launcher = launcher
        w = Win(tc)
        assert w.send("connect")["ok"]
        info = wait_seq(app)
        assert info.status == "failed"
        assert info.error == "Start PWI4 failed: PWI4 executable not found (set PW_PWI4_EXE)"
        w.close()


def test_launcher_exception_is_a_failed_step():
    fake = disconnected_fake()
    fake.down = True
    with running(fake, wait_ready=False) as (tc, _, app):
        ctl = app.state.control
        wait_for(lambda: ctl._startup_deferred, 5)

        def launcher():
            raise OSError("access denied")

        ctl.seq.launcher = launcher
        w = Win(tc)
        assert w.send("connect")["ok"]
        assert "Start PWI4 failed: could not start PWI4" in wait_seq(app).error
        w.close()


def test_find_pwi4_exe_override_then_candidates(tmp_path, monkeypatch):
    from gateway import sequences

    exe = tmp_path / "PWI4.exe"
    exe.write_text("x")
    assert find_pwi4_exe(str(exe)) == str(exe)
    assert find_pwi4_exe(str(tmp_path / "nope.exe")) is None or "PlaneWave" in find_pwi4_exe(
        str(tmp_path / "nope.exe")
    )
    first = tmp_path / "x86" / "PWI4.exe"
    second = tmp_path / "x64" / "PWI4.exe"
    for f in (first, second):
        f.parent.mkdir()
        f.write_text("x")
    monkeypatch.setattr(sequences, "PWI4_EXE_CANDIDATES", (str(first), str(second)))
    assert find_pwi4_exe() == str(first)
    first.unlink()
    assert find_pwi4_exe() == str(second)
    second.unlink()
    assert find_pwi4_exe() is None


def test_launch_pwi4_never_spawns_when_exe_missing(monkeypatch):
    from gateway import sequences

    monkeypatch.setattr(sequences, "PWI4_EXE_CANDIDATES", ())
    popen_called = []
    monkeypatch.setattr(sequences.subprocess, "Popen", lambda *a, **k: popen_called.append(1))
    with pytest.raises(StepError, match="not found"):
        sequences.launch_pwi4(None)
    assert popen_called == []


def test_launch_pwi4_skips_when_already_running(tmp_path, monkeypatch):
    from gateway import sequences

    exe = tmp_path / "PWI4.exe"
    exe.write_text("x")
    popen_called = []
    monkeypatch.setattr(sequences.subprocess, "Popen", lambda *a, **k: popen_called.append(1))
    monkeypatch.setattr(sequences, "pwi4_process_running", lambda: True)
    out = sequences.launch_pwi4(str(exe))
    assert out["launched"] is False and popen_called == []


def test_launch_pwi4_detached_popen_args(tmp_path, monkeypatch):
    from gateway import sequences

    exe = tmp_path / "PWI4.exe"
    exe.write_text("x")
    seen = {}

    def fake_popen(args, **kw):
        seen.update(args=args, **kw)
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(sequences.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sequences, "pwi4_process_running", lambda: False)
    out = sequences.launch_pwi4(str(exe))
    assert out == {"exe": str(exe), "launched": True, "pid": 4242}
    assert seen["args"] == [str(exe)] and seen["close_fds"] is True


# ------------------------------------------------------------------ failure injection


def _fail_connect(over_fake: FakePwi4, fail: set[str]):
    over_fake.fail_writes = fail
    return over_fake


@pytest.mark.parametrize(
    ("fail", "label", "mount_connected_after"),
    [
        ("/mount/connect", "Connect mount", False),
        ("/mount/enable", "Enable axes", True),
        ("/mount/find_home", "Home mount", True),
    ],
)
def test_connect_failure_at_each_step(fail, label, mount_connected_after, tmp_path):
    fake = disconnected_fake()
    fake.fail_writes = {fail}
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        info = wait_seq(app)
        assert info.status == "failed"
        assert info.error.startswith(f"{label} failed: ") and "HTTP 500" in info.error
        assert info.step == label.lower()
        p = paths(fake)
        # nothing after the failing step ran
        order = ["/mount/connect", "/mount/enable", "/mount/find_home"]
        for later in order[order.index(fail) + 1 :]:
            assert later not in p
        # no rollback: the mount is never disconnected
        assert "/mount/disconnect" not in p
        # safe: when the mount is connected both axes were disabled
        if mount_connected_after:
            assert p.count("/mount/disable") == 2 and "/mount/stop" in p
        else:
            assert "/mount/disable" not in p
        assert state._override is None
        w.close()
    ev = {e["event"]: e for e in audit_lines(tmp_path)}
    assert ev["sequence_failed"]["step"] == label.lower()
    assert "sequence_safe" in ev


def test_connect_failure_mount_never_connects_times_out():
    fake = disconnected_fake()
    fake.connect_noop = True
    with running(fake) as (tc, _, app):
        vc = virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        info = wait_seq(app)
        assert info.error == "Connect mount failed: the mount did not connect"
        assert vc.t >= 30.0
        assert "/mount/enable" not in paths(fake)
        w.close()


def test_connect_failure_axes_never_enable_times_out():
    fake = disconnected_fake()
    fake.set(mount__is_connected=True)
    orig = fake._apply_write

    def no_enable(path, params=None):
        if path != "/mount/enable":
            orig(path, params)

    fake._apply_write = no_enable  # type: ignore[method-assign]
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        assert wait_seq(app).error == "Enable axes failed: the axes did not enable"
        w.close()


def test_connect_write_timeout_is_a_failed_step():
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        virtual(app)
        fake.write_delay = 0.3
        orig = app.state.control.writer._call

        async def short(path, _timeout=1.0, **params):
            return await orig(path, 0.1, **params)

        app.state.control.writer._call = short  # type: ignore[method-assign]
        w = Win(tc)
        assert w.send("connect")["ok"]
        info = wait_seq(app)
        assert info.status == "failed" and "did not answer in time" in info.error
        w.close()


@pytest.mark.parametrize(
    ("fail", "label"),
    [
        ("/mount/stop", "Stop motion"),
        ("/mount/find_home", "Home mount"),
        ("/mount/disable", "Disable axes"),
        ("/mount/disconnect", "Disconnect mount"),
    ],
)
def test_disconnect_failure_at_each_step(fail, label):
    fake = FakePwi4()
    fake.fail_writes = {fail}
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("disconnect")["ok"]
        info = wait_seq(app)
        assert info.status == "failed"
        assert info.error.startswith(f"{label} failed: ") and "HTTP 500" in info.error
        p = paths(fake)
        # nothing after the failing step ran (the axes are disabled by the make-safe tail,
        # which is why /mount/disable is not in this list)
        later_steps = {
            "/mount/stop": ["/mount/find_home", "/mount/disconnect"],
            "/mount/find_home": ["/mount/disconnect"],
            "/mount/disable": ["/mount/disconnect"],
            "/mount/disconnect": [],
        }[fail]
        for later in later_steps:
            assert later not in p, (later, p)
        assert "/mount/enable" not in p  # no rollback
        if fail != "/mount/disconnect":
            assert p.count("/mount/disable") >= 2  # made safe: both axes disabled
        assert state._override is None
        w.close()


def test_disconnect_failure_leaves_axes_disabled():
    fake = FakePwi4()
    fake.fail_writes = {"/mount/find_home"}
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("disconnect")["ok"]
        assert wait_seq(app).status == "failed"
        st = app.state.control.fresh_status() or app.state.poller.last_good.status
        wait_for(
            lambda: (
                not app.state.poller.last_good.status.mount.axis0.is_enabled
                and not app.state.poller.last_good.status.mount.axis1.is_enabled
            )
        )
        assert st.mount.is_connected  # not disconnected
        w.close()


# ------------------------------------------------------------------ STOP / link loss / lock


def test_stop_mid_sequence_cancels_and_clears_busy():
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        a, b = Win(tc), Win(tc)
        fake.write_delay = 0.6  # PWI4 hangs on mount/connect
        assert a.send("connect")["ok"]
        time.sleep(0.25)
        fake.write_delay = 0.0
        t0 = time.monotonic()
        ack = b.send("stop", cid="s")  # STOP from ANY window
        assert ack["ok"] and time.monotonic() - t0 < 0.5
        info = wait_seq(app)
        assert info.status == "cancelled" and info.error == "Cancelled by STOP"
        time.sleep(0.8)
        p = paths(fake)
        assert "/mount/enable" not in p and "/mount/find_home" not in p
        assert "/mount/stop" in p
        assert state._override is None and not app.state.control.seq.active
        # a new sequence can start afterwards
        assert a.send("connect", cid="c2")["ok"]
        assert wait_seq(app).status == "done"
        a.close()
        b.close()


def test_stop_during_home_cancels_it(tmp_path):
    fake = FakePwi4()
    script = [(0.2, {})] + [
        (0.1, {"mount__axis0__position_degs": 120 + 5 * i, "mount__azimuth_degs": 120 + 5 * i})
        for i in range(1, 60)
    ]
    fake.home_script = script
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("home")["ok"]
        wait_for(lambda: "/mount/find_home" in paths(fake))
        time.sleep(0.6)
        assert w.send("stop", cid="s")["ok"]
        info = wait_seq(app)
        assert info.status == "cancelled"
        assert state._override is None
        assert app.state.monitor.homing is None
        w.close()


def test_link_loss_cancels_sequence_and_stops(tmp_path):
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        a, b = Win(tc), Win(tc)
        fake.write_delay = 0.5
        assert a.send("connect")["ok"]
        time.sleep(0.2)
        fake.write_delay = 0.0
        a.close()
        info = wait_seq(app)
        assert info.status == "cancelled" and info.error == "Cancelled: control link lost"
        wait_for(lambda: app.state.control.holder is None)
        assert "/mount/enable" not in paths(fake)
        b.close()


def test_lock_rules_and_busy_refusals():
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        a, b = Win(tc), Win(tc)
        fake.write_delay = 0.4
        assert a.send("connect")["ok"]  # first command takes control
        a.expect_control("you")
        assert b.send("connect", cid="b1")["reason"] == "Another window has control"
        assert b.send("disconnect", cid="b2")["reason"] == "Another window has control"
        assert b.send("home", cid="b3")["reason"] == "Another window has control"
        assert a.send("connect", cid="a2")["reason"] == "Busy: connecting"
        assert a.send("goto_altaz", {"alt": 40, "az": 125}, "a3")["reason"] == "Busy: connecting"
        assert a.send("tracking", {"on": True}, "a4")["reason"] == "Busy: connecting"
        assert b.send("stop", cid="s")["ok"]  # STOP from anyone
        wait_seq(app)
        a.close()
        b.close()


def test_take_control_during_sequence_does_not_stop_it():
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        a, b = Win(tc), Win(tc)
        fake.write_delay = 0.2
        assert a.send("connect")["ok"]
        assert b.send("take_control", cid="t")["ok"]
        assert wait_seq(app).status == "done"
        a.close()
        b.close()


def test_sequences_need_pwi4_and_mount_state():
    fake = disconnected_fake()  # mount not connected
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("home", cid="h")["reason"] == "Mount not connected"
        assert w.send("disconnect", cid="d")["reason"] == "Mount not connected"
        assert fake.writes == []
        w.close()
    fake2 = FakePwi4()
    fake2.set(mount__axis1__is_enabled=False)
    with running(fake2) as (tc, _, app):
        w = Win(tc)
        assert w.send("home", cid="h")["reason"] == "Axis 1 disabled"
        assert fake2.writes == []
        w.close()


def test_disconnect_with_axes_disabled_skips_home_with_warning():
    fake = FakePwi4()
    fake.set(mount__axis0__is_enabled=False, mount__axis1__is_enabled=False)
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("disconnect")["ok"]
        info = wait_seq(app)
        assert info.status == "done" and info.warning == "Home skipped: axes are disabled"
        p = paths(fake)
        assert "/mount/find_home" not in p and "/mount/disable" not in p
        assert p[-1] == "/mount/disconnect"
        w.close()


# ------------------------------------------------------------------ Sun-blocked home


def test_connect_sun_blocked_skips_home_with_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", SUN_ON_HOME_PATH)
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        assert w.send("connect")["ok"]
        info = wait_seq(app)
        assert info.status == "done" and info.error is None
        assert info.warning == "Home skipped: path passes within 15° of the Sun"
        p = paths(fake)
        assert "/mount/find_home" not in p and "/mount/disable" not in p
        assert p.count("/mount/enable") == 2
        wait_for(lambda: app.state.poller.latest.state == "STOPPED")  # connected, axes enabled
        assert app.state.poller.latest.sequence.warning == info.warning
        w.close()
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "home_skipped"]
    assert ev and ev[0]["sun"] is True


def test_disconnect_sun_blocked_confirm_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", SUN_ON_HOME_PATH)
    fake = FakePwi4()
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        ack = w.send("disconnect", cid="d1")
        assert ack["ok"] is False and ack["code"] == "home_blocked_confirm"
        assert ack["reason"] == "Home is blocked by the Sun; disconnect without homing?"
        assert fake.writes == []  # nothing happened
        assert seq_info(app) is None
        # "No": nothing changes (the client simply does not resend)
        ack2 = w.send("disconnect", {"skip_home": True}, "d2")
        assert ack2["ok"] is True
        info = wait_seq(app)
        assert info.status == "done" and "without homing" in info.warning
        p = paths(fake)
        assert "/mount/find_home" not in p
        assert p.count("/mount/disable") == 2 and p[-1] == "/mount/disconnect"
        w.close()
    ev = [e["event"] for e in audit_lines(tmp_path)]
    assert "disconnect_without_home" in ev
    refused = [e for e in audit_lines(tmp_path) if e["event"] == "cmd" and e.get("code")]
    assert refused and refused[0]["code"] == "home_blocked_confirm"


def test_home_command_sun_blocked_is_refused_no_write(monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", SUN_ON_HOME_PATH)
    with running() as (tc, fake, app):
        w = Win(tc)
        ack = w.send("home")
        assert ack["ok"] is False and ack["reason"] == "Slew path passes within 15° of the Sun"
        assert fake.writes == []
        w.close()


def test_skip_home_is_ignored_when_home_is_not_blocked():
    with running() as (tc, fake, app):  # Sun far below the horizon: shortcut applies
        virtual(app)
        w = Win(tc)
        assert w.send("disconnect", {"skip_home": True})["ok"]
        assert wait_seq(app).status == "done"
        assert "/mount/find_home" in paths(fake)
        w.close()


def test_home_blocked_after_precheck_fails_the_step(monkeypatch):
    """The Sun moved between the pre-check and the HOME step: the step re-checks."""
    fake = FakePwi4()
    with running(fake) as (tc, _, app):
        virtual(app)
        ctl = app.state.control
        real = ctl.home_check
        calls = {"n": 0}

        def flaky(st):
            calls["n"] += 1
            return HomeVerdict() if calls["n"] == 1 else HomeVerdict("Slew path blocked", True)

        ctl.home_check = flaky
        w = Win(tc)
        assert w.send("disconnect")["ok"]  # pre-check passes
        info = wait_seq(app)
        assert info.status == "failed" and info.error.startswith("Home mount failed: ")
        assert "/mount/find_home" not in paths(fake)
        ctl.home_check = real
        w.close()


# ------------------------------------------------------------------ HOME safety check (pure)


def make_engine(tmp_path, sun: tuple[float, float] | None, hw: dict | None = None):
    store = SettingsStore(tmp_path / "s.json", MemorySecretStore())
    s = StationSettings.model_validate(
        {
            **StationSettings().model_dump(),
            "hardware": {**StationSettings().hardware.model_dump(), **(hw or {})},
        }
    )
    s.location.lat_deg, s.location.lon_deg, s.location.alt_m = 14.3479, 100.5656, 12.0
    store.current = s
    from gateway.audit import AuditLog

    return SafetyEngine(None, store, Ephem(tmp_path / "none.bsp"), AuditLog(tmp_path / "a"), sun)  # type: ignore[arg-type]


def st_at(az=120.0, alt=45.0, **extra):
    over = {
        "mount.azimuth_degs": az,
        "mount.altitude_degs": alt,
        "mount.axis0.position_degs": az,
        "mount.axis1.position_degs": alt,
    }
    over.update(extra)
    return parse(status_text(over))


def test_check_home_allowed_with_shortcut(tmp_path):
    eng = make_engine(tmp_path, (-60.0, 0.0))
    v = eng.check_home(st_at())
    assert v.reason is None and not v.sun_blocked


def test_check_home_sun_on_path_blocked(tmp_path):
    eng = make_engine(tmp_path, (65.0, 150.0))
    v = eng.check_home(st_at())
    assert v.reason == "Slew path passes within 15° of the Sun" and v.sun_blocked


def test_check_home_path_clear_of_sun_allowed(tmp_path):
    eng = make_engine(tmp_path, (30.0, 300.0))  # Sun on the other side of the sky
    v = eng.check_home(st_at())
    assert v.reason is None and v.start_sun_sep is not None and v.start_sun_sep > 15


def test_check_home_pose_inside_disc_blocked(tmp_path):
    eng = make_engine(tmp_path, (80.0, 175.0))  # the Sun sits at the home pose
    v = eng.check_home(st_at(az=170.0, alt=70.0))
    assert v.reason == "Within 15° of the Sun" and v.sun_blocked


def test_check_home_recovery_when_already_inside_and_not_closer(tmp_path):
    # mount AT home (85/180) with the Sun 10 deg away: a zero-length path, never closer
    eng = make_engine(tmp_path, (85.0, 180.0))
    v = eng.check_home(st_at(az=180.0, alt=85.0))
    assert v.reason == "Within 15° of the Sun"  # the pose itself is inside the disc: blocked
    # start inside the disc (10 deg from the Sun) but the path only moves away from it
    eng2 = make_engine(tmp_path, (30.0, 100.0))
    v2 = eng2.check_home(st_at(az=100.0, alt=40.0))
    assert v2.reason is None


def test_check_home_uses_settings_home_pose(tmp_path):
    eng = make_engine(tmp_path, (65.0, 150.0), {"home_alt_deg": 60.0, "home_az_deg": 20.0})
    assert eng.home_pose() == (60.0, 20.0)
    # mount at az 120 / alt 45 -> home 60 / 20 sweeps away from the Sun at 65/150? It starts
    # 30 deg from the Sun and moves away: allowed
    assert eng.check_home(st_at()).reason is None


def test_check_home_other_refusals(tmp_path):
    eng = make_engine(tmp_path, (-60.0, 0.0), {"home_alt_deg": 10.0})
    assert "below the station limit" in eng.check_home(st_at()).reason
    eng = make_engine(tmp_path, (-60.0, 0.0))
    eng.store.current.location.lat_deg = 40.0  # site differs from PWI4's
    assert eng.check_home(st_at()).reason == "Station location differs from PWI4's site"
    eng = make_engine(tmp_path, (-60.0, 0.0))
    v = eng.check_home(
        st_at(
            **{"mount.axis0.min_mech_position_degs": 0, "mount.axis0.max_mech_position_degs": 100}
        )
    )
    assert v.reason == "Target azimuth is outside the mount's mechanical range"


# ------------------------------------------------------------------ homing watch (monitor)


def test_actual_limit_decision_rules():
    # Sun: actual separation <= 15 stops; start inside -> deeper than the start stops
    assert actual_limit_decision(45, 120, 15.0, (45, 130), 45, None)[0] == "Within 15° of the Sun"
    assert actual_limit_decision(45, 120, 15.0, (45, 150), 45, None) is None
    inside_start = 7.0  # started 7 deg from the Sun: only getting deeper than that stops
    assert actual_limit_decision(45, 120, 15.0, (45, 130), 45, inside_start) is None  # 7.07
    assert actual_limit_decision(45, 120, 15.0, (45, 126), 45, inside_start)[0] == (
        "Within 15° of the Sun"
    )
    # Lim EL: floor is min(Lim EL, start altitude)
    assert actual_limit_decision(14.9, 0, 15.0, None, 45, None)[0].startswith("Below station")
    assert actual_limit_decision(14.96, 0, 15.0, None, 45, None) is None  # within the margin
    assert actual_limit_decision(8.96, 0, 15.0, None, 9.0, None) is None  # recovery start
    assert actual_limit_decision(8.9, 0, 15.0, None, 9.0, None) is not None  # deeper: stop


def make_monitor(tmp_path, sun, fake):
    """A SafetyMonitor fed from a FakePwi4 status (tick() does no I/O)."""
    eng = make_engine(tmp_path, sun)
    holder = {"st": st_at()}
    ctl = SimpleNamespace(
        fresh_status=lambda: holder["st"],
    )
    poller = SimpleNamespace(last_good_t=0.0)
    mon = SafetyMonitor(eng, poller, ctl)  # type: ignore[arg-type]

    def feed(az, alt, dt=0.1):
        poller.last_good_t += dt
        holder["st"] = st_at(az=az, alt=alt)
        return mon.tick()

    return mon, feed


def test_homing_watch_stops_on_actual_sun_distance_only(tmp_path):
    mon, feed = make_monitor(tmp_path, (45.0, 160.0), None)
    mon.homing = HomingWatch(start_alt=45.0, start_sun_sep=None)
    # closing on the Sun but still > 15 deg away: the PREDICTIVE rule is off while homing
    decisions = [feed(120 + 15 * i, 45) for i in range(6)]  # 150 deg/s synthetic fast approach
    assert all(d is None or d[0] == "Within 15° of the Sun" for d in decisions)
    first = next(i for i, d in enumerate(decisions) if d)
    az_at = 120 + 15 * first
    assert abs(az_at - 160) <= 15.0 + 1e-6  # only when ACTUALLY inside the disc


def test_homing_watch_does_not_use_predictive_or_corridor_rules(tmp_path):
    mon, feed = make_monitor(tmp_path, (-60.0, 0.0), None)
    mon.homing = HomingWatch(start_alt=45.0)
    # fast descent 45 -> 20 within the limits: the predictive look-ahead would have stopped it
    ds = [feed(120, 45 - 5 * i) for i in range(6)]
    assert all(d is None for d in ds)
    # but actually below Lim EL: stop
    assert feed(120, 14.0) is not None


def test_homing_watch_cleared_by_clear_and_end(tmp_path):
    mon, _ = make_monitor(tmp_path, (-60.0, 0.0), None)
    mon.homing = HomingWatch(start_alt=45.0)
    mon.clear()
    assert mon.homing is None
    mon.homing = HomingWatch(start_alt=45.0)
    mon.end_homing()
    assert mon.homing is None


def test_monitor_stops_a_real_home_that_enters_the_sun_disc(monkeypatch, tmp_path):
    """End to end on FakePwi4: the sun-check passes at the start (Sun far from the straight
    path), then the 'real' homing path swings through the disc -> limit_stop + cancelled."""
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "50,300")  # far from the predicted path
    fake = FakePwi4()
    # the actual homing path wanders toward az 300 / alt 50 (not what the predictor assumed)
    script: list[tuple[float, dict]] = [(0.2, {})]
    for i in range(1, 40):
        az = 120 + 6 * i
        script.append(
            (
                0.1,
                {
                    "mount__axis0__position_degs": az,
                    "mount__azimuth_degs": az,
                    "mount__is_slewing": True,
                },
            )
        )
    fake.home_script = script
    with running(fake) as (tc, _, app):
        w = Win(tc)
        assert w.send("home")["ok"]
        info = wait_seq(app, 30)
        assert info.status == "cancelled" and info.error == "Cancelled: Within 15° of the Sun"
        assert "/mount/stop" in paths(fake)
        assert app.state.monitor.homing is None
        w.close()
    ev = [e for e in audit_lines(tmp_path) if e["event"] == "limit_stop"]
    assert ev and ev[0]["reason"] == "Within 15° of the Sun"


# ------------------------------------------------------------------ settings: home pose


def test_settings_home_pose_defaults_and_old_files(tmp_path):
    p = tmp_path / "s.json"
    old = StationSettings().model_dump()
    del old["hardware"]["home_alt_deg"], old["hardware"]["home_az_deg"]
    p.write_text(json.dumps(old))
    st = SettingsStore(p, MemorySecretStore())
    assert st.load_error is None
    assert (st.current.hardware.home_alt_deg, st.current.hardware.home_az_deg) == (85.0, 180.0)


def test_settings_home_pose_hand_edit_survives_put_without_it(tmp_path):
    p = tmp_path / "s.json"
    d = StationSettings().model_dump()
    d["hardware"]["home_alt_deg"], d["hardware"]["home_az_deg"] = 80.0, 170.0
    p.write_text(json.dumps(d))
    st = SettingsStore(p, MemorySecretStore())
    assert st.current.hardware.home_alt_deg == 80.0
    # the Settings screen's PUT carries no home fields
    from gateway.settings import SettingsUpdate

    body = StationSettings().model_dump()
    body["location"]["timezone"] = "UTC"
    del body["hardware"]["home_alt_deg"], body["hardware"]["home_az_deg"]
    body["hardware"]["lim_el_deg"] = 20.0
    st.save(SettingsUpdate.model_validate(body))
    assert st.current.hardware.lim_el_deg == 20.0
    assert (st.current.hardware.home_alt_deg, st.current.hardware.home_az_deg) == (80.0, 170.0)
    saved = json.loads(p.read_text())
    assert saved["hardware"]["home_alt_deg"] == 80.0 and saved["hardware"]["home_az_deg"] == 170.0
    # an explicit value is honoured
    body["hardware"]["home_alt_deg"] = 75.0
    st.save(SettingsUpdate.model_validate(body))
    assert st.current.hardware.home_alt_deg == 75.0 and st.current.hardware.home_az_deg == 170.0


@pytest.mark.parametrize("hw", [{"home_alt_deg": 91}, {"home_alt_deg": -1}, {"home_az_deg": 360}])
def test_settings_home_pose_validation(hw):
    from pydantic import ValidationError

    from gateway.settings import Hardware

    with pytest.raises(ValidationError):
        Hardware(**hw)


# ------------------------------------------------------------------ misc


def test_snapshot_sequence_none_before_any_sequence():
    with running() as (tc, _, app):
        wait_for(lambda: app.state.poller.latest is not None)
        assert tc.get("/api/mount/status").json()["sequence"] is None


def test_only_whitelisted_write_endpoints_with_sequences():
    allowed = {
        "/mount/stop",
        "/mount/tracking_on",
        "/mount/tracking_off",
        "/mount/goto_alt_az",
        "/mount/goto_ra_dec_j2000",
        "/mount/set_axis0_wrap_range_min",
        "/mount/connect",
        "/mount/disconnect",
        "/mount/enable",
        "/mount/disable",
        "/mount/find_home",
    }
    fake = disconnected_fake()
    with running(fake) as (tc, _, app):
        virtual(app)
        w = Win(tc)
        w.send("connect", cid="1")
        wait_seq(app)
        w.send("home", cid="2")
        wait_seq(app)
        w.send("disconnect", cid="3")
        wait_seq(app)
        w.close()
    assert {p for p, _ in fake.writes} <= allowed
    assert not any("settings" in p or "site" in p or "park" in p for p, _ in fake.writes)


def test_pwi4_error_type_is_shared():
    assert issubclass(Pwi4Error, Exception)
