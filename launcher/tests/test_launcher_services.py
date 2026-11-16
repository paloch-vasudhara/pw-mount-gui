"""T6.2b launcher: paths, ports, health, job object, graceful stop, mutex, MediaMTX, logs."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from launcher import paths, services
from launcher.services import Child
from launcher.winjob import Job, Mutex

HERE = Path(__file__).parent
TEST_PORT = 18761  # throwaway range 18761-18769 only
FLAGS = services.CREATE_NO_WINDOW | services.CREATE_NEW_PROCESS_GROUP


def _alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
    ).stdout
    return str(pid) in out


def _wait(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(autouse=True)
def _tmp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for k in ("PW_WEB_DIR", "PW_LOG_DIR", "PW_SETTINGS_PATH"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- paths


def test_web_dir_repo_package_and_env(tmp_path, monkeypatch):
    assert paths.web_dir(tmp_path) is None
    pkg = tmp_path / "web"
    pkg.mkdir()
    (pkg / "index.html").write_text("x")
    assert paths.web_dir(tmp_path) == pkg
    repo = tmp_path / "web" / ".output" / "public"
    repo.mkdir(parents=True)
    (repo / "index.html").write_text("x")
    assert paths.web_dir(tmp_path) == repo  # the repo build wins
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("PW_WEB_DIR", str(other))
    assert paths.web_dir(tmp_path) == other
    monkeypatch.setenv("PW_WEB_DIR", str(tmp_path / "missing"))
    assert paths.web_dir(tmp_path) is None


def test_icon_repo_and_package(tmp_path):
    assert paths.icon_path(tmp_path) is None
    (tmp_path / "app.ico").write_bytes(b"i")
    assert paths.icon_path(tmp_path) == tmp_path / "app.ico"
    d = tmp_path / "tools" / "package" / "icon"
    d.mkdir(parents=True)
    (d / "app.ico").write_bytes(b"i")
    assert paths.icon_path(tmp_path) == d / "app.ico"
    assert paths.icon_path() is not None  # the real repo icon


def test_app_root_is_parent_of_launcher():
    assert (paths.APP_ROOT / "launcher").is_dir()
    assert (paths.APP_ROOT / "gateway").is_dir()


def test_log_dir_under_localappdata(tmp_path):
    assert paths.log_dir() == tmp_path / "local" / "pw-mount-control" / "logs"


# ---------------------------------------------------------------- ports


def test_pick_port_default_then_next():
    assert services.pick_port(TEST_PORT, TEST_PORT + 1, TEST_PORT + 3) == TEST_PORT
    s = socket.socket()
    s.bind(("127.0.0.1", TEST_PORT))
    s.listen()
    try:
        assert services.pick_port(TEST_PORT, TEST_PORT + 1, TEST_PORT + 3) == TEST_PORT + 1
        assert services.pick_port(TEST_PORT, TEST_PORT + 1, TEST_PORT + 3, (TEST_PORT + 1,)) == (
            TEST_PORT + 2
        )
        assert services.port_listening(TEST_PORT)
    finally:
        s.close()
    assert not services.port_listening(TEST_PORT)


def test_pick_port_none_when_all_busy():
    socks = []
    try:
        for p in (TEST_PORT, TEST_PORT + 1):
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            socks.append(s)
        assert services.pick_port(TEST_PORT, TEST_PORT + 1, TEST_PORT + 1) is None
    finally:
        for s in socks:
            s.close()


# ---------------------------------------------------------------- health


class _FakeChild:
    def __init__(self, alive=True):
        self._alive = alive

    def alive(self):
        return self._alive


def test_wait_healthy_ok_after_retries():
    calls = []

    def check(url):
        calls.append(url)
        return len(calls) >= 3

    assert services.wait_healthy("u", None, check=check, sleep=lambda s: None)
    assert len(calls) == 3


def test_wait_healthy_timeout():
    t = [0.0]

    def sleep(s):
        t[0] += s

    ok = services.wait_healthy(
        "u", None, timeout=5, poll=0.5, check=lambda u: False, sleep=sleep, clock=lambda: t[0]
    )
    assert not ok
    assert 5 <= t[0] <= 5.5


def test_wait_healthy_early_exit_is_immediate():
    calls = []
    ok = services.wait_healthy(
        "u", _FakeChild(False), check=lambda u: calls.append(u) or True, sleep=lambda s: None
    )
    assert not ok and not calls


def test_http_ok_real_loopback_marker(tmp_path):
    child = services.spawn(
        "marker",
        [sys.executable, str(HERE / "marker_app.py"), str(TEST_PORT), str(tmp_path / "m.txt")],
        paths.APP_ROOT,
        os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
        tmp_path / "marker.out.log",
        None,
    )
    try:
        assert services.wait_healthy(f"http://127.0.0.1:{TEST_PORT}/api/health", child, 30)
        assert not services.http_ok(f"http://127.0.0.1:{TEST_PORT}/nope")
    finally:
        services.stop_child(child, 5)
        assert not _alive(child.pid)


# ---------------------------------------------------------------- job object


def test_job_close_kills_child():
    job = Job()
    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        creationflags=FLAGS,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        job.assign(p.pid)
        assert p.poll() is None
        job.close()
        assert _wait(lambda: p.poll() is not None, 10)
    finally:
        job.close()
        if p.poll() is None:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)


# ---------------------------------------------------------------- graceful stop


def test_graceful_stop_runs_lifespan_shutdown(tmp_path):
    """CTRL_BREAK reaches uvicorn on Windows and its lifespan shutdown (G4) runs."""
    marker = tmp_path / "marker.txt"
    child = services.spawn(
        "marker",
        [sys.executable, str(HERE / "marker_app.py"), str(TEST_PORT + 1), str(marker)],
        paths.APP_ROOT,
        os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
        tmp_path / "marker.out.log",
        None,
    )
    try:
        assert services.wait_healthy(f"http://127.0.0.1:{TEST_PORT + 1}/api/health", child, 30)
        assert not marker.exists()
        how = services.stop_child(child, 10)
    finally:
        if child.alive():
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
    assert how == "graceful"
    assert marker.read_text() == "shutdown ran"


def test_stop_child_terminates_when_ctrl_break_is_ignored(tmp_path):
    code = (
        "import signal,time;signal.signal(signal.SIGBREAK, signal.SIG_IGN);print('up',flush=True);"
        "time.sleep(120)"
    )
    child = services.spawn(
        "ign",
        [sys.executable, "-c", code],
        paths.APP_ROOT,
        dict(os.environ),
        tmp_path / "ign.out.log",
        None,
    )
    try:
        assert _wait(lambda: "up" in services.tail_lines(child.log_path))
        how = services.stop_child(child, 1.0)
        assert how == "terminated"
        assert not child.alive()
    finally:
        if child.alive():
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)


def test_stop_child_already_exited(tmp_path):
    child = services.spawn(
        "x",
        [sys.executable, "-c", "pass"],
        paths.APP_ROOT,
        dict(os.environ),
        tmp_path / "x.out.log",
        None,
    )
    child.proc.wait(10)
    assert services.stop_child(child, 1) == "already exited"


# ---------------------------------------------------------------- mutex


def test_mutex_second_acquire_fails():
    name = f"Local\\PWMountControlTest{os.getpid()}"
    a = Mutex(name)
    b = Mutex(name)
    try:
        assert a.acquired
        assert not b.acquired
    finally:
        b.close()
        a.close()
    c = Mutex(name)
    try:
        assert c.acquired
    finally:
        c.close()


# ---------------------------------------------------------------- MediaMTX


def _settings(tmp_path, cctv):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"cctv": cctv}), encoding="utf-8")
    return f


@pytest.mark.parametrize(
    "cctv,folder,busy,expect",
    [
        ({"enabled": True, "type": "hls"}, True, False, True),
        ({"enabled": False, "type": "hls"}, True, False, False),
        ({"enabled": True, "type": "mjpeg"}, True, False, False),
        ({"enabled": True, "type": "hls"}, False, False, False),
        ({"enabled": True, "type": "hls"}, True, True, False),
        ({}, True, False, False),
    ],
)
def test_mediamtx_decision_table(tmp_path, cctv, folder, busy, expect):
    ok, reason = services.mediamtx_decision(
        _settings(tmp_path, cctv), tmp_path if folder else None, lambda p: busy
    )
    assert ok is expect
    assert reason


def test_mediamtx_decision_no_settings_file(tmp_path):
    assert services.mediamtx_decision(tmp_path / "none.json", tmp_path, lambda p: False)[0] is False


def test_mediamtx_folder_relay_and_launcher_json(tmp_path):
    assert paths.mediamtx_folder(tmp_path) is None
    relay = tmp_path / "relay"
    relay.mkdir()
    (relay / "mediamtx.exe").write_bytes(b"x")
    assert paths.mediamtx_folder(tmp_path) is None  # yml missing
    (relay / "mediamtx.yml").write_text("x")
    assert paths.mediamtx_folder(tmp_path) == relay
    other = tmp_path / "other"
    other.mkdir()
    (other / "mediamtx.exe").write_bytes(b"x")
    (other / "mediamtx.yml").write_text("x")
    root2 = tmp_path / "root2"
    root2.mkdir()
    d = paths.data_dir()
    d.mkdir(parents=True)
    (d / "launcher.json").write_text(json.dumps({"mediamtx": str(other)}))
    assert paths.mediamtx_folder(root2) == other


def test_settings_path_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PW_SETTINGS_PATH", str(tmp_path / "s.json"))
    assert paths.settings_path() == tmp_path / "s.json"


# ---------------------------------------------------------------- logs


def test_child_log_rotation_keeps_previous_runs(tmp_path):
    p = tmp_path / "gateway.out.log"
    for i in range(7):
        p.write_text(f"run{i}")
        services.rotate_log(p)
    assert not p.exists()
    assert [(tmp_path / f"gateway.out.log.{i}").read_text() for i in (1, 2, 3, 4)] == [
        "run6",
        "run5",
        "run4",
        "run3",
    ]
    assert not (tmp_path / "gateway.out.log.5").exists()


def test_launcher_log_rotates_5_files_of_2mb(tmp_path):
    path = services.setup_logging(tmp_path / "logs")
    h = logging.getLogger("launcher").handlers[0]
    assert h.maxBytes == 2 * 1024 * 1024 and h.backupCount == 4
    assert path.name == "launcher.log"


def test_launcher_log_has_no_env_or_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PW_TOKEN_SECRET_TEST", "s3cr3t-value-123")
    monkeypatch.setenv(
        "PW_SETTINGS_PATH", str(_settings(tmp_path, {"enabled": True, "url": "rtsp://u:pw@h"}))
    )
    path = services.setup_logging(tmp_path / "logs")
    services.mediamtx_decision(paths.settings_path(), None)
    child = services.spawn(
        "dummy",
        [sys.executable, "-c", "pass"],
        paths.APP_ROOT,
        dict(os.environ),
        tmp_path / "d.out.log",
        None,
    )
    child.proc.wait(10)
    services.stop_child(child, 1)
    for h in logging.getLogger("launcher").handlers:
        h.flush()
    text = path.read_text(encoding="utf-8")
    assert "dummy started" in text
    assert "s3cr3t-value-123" not in text and "rtsp://" not in text


def test_tail_lines(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("\n".join(str(i) for i in range(100)) + "\n")
    assert services.tail_lines(f, 30).splitlines() == [str(i) for i in range(70, 100)]
    assert services.tail_lines(tmp_path / "none.log") == ""


def test_child_is_child_dataclass():
    assert Child.__dataclass_fields__["name"]
