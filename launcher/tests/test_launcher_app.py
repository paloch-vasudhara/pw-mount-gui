"""T6.2b launcher: window state, start-up failure path, single instance, main flow (no windows)."""

from __future__ import annotations

import os
import sys

import pytest

from launcher import __main__ as entry
from launcher import paths, runner, services, ui, windowstate
from launcher.winjob import Mutex

SCREEN = (0, 0, 1920, 1080)


@pytest.fixture(autouse=True)
def _tmp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for k in ("PW_WEB_DIR", "PW_LOG_DIR", "PW_SETTINGS_PATH"):
        monkeypatch.delenv(k, raising=False)
    # never the real single-instance name: a running launcher must not interfere
    monkeypatch.setattr(entry, "MUTEX_NAME", f"Local\\PWMountControlTest{os.getpid()}")


def test_window_state_roundtrip(tmp_path):
    f = tmp_path / "window.json"
    windowstate.save(f, {"x": 100, "y": 80, "width": 1500, "height": 900, "maximized": True})
    assert windowstate.load(f, SCREEN) == {
        "x": 100,
        "y": 80,
        "width": 1500,
        "height": 900,
        "maximized": True,
    }


def test_window_state_defaults_and_min_size(tmp_path):
    s = windowstate.load(tmp_path / "none.json", SCREEN)
    assert (s["width"], s["height"], s["maximized"]) == (*windowstate.DEFAULT_SIZE, False)
    assert "x" not in s
    s = windowstate.sanitize({"width": 800, "height": 300, "x": 0, "y": 0}, SCREEN)
    assert (s["width"], s["height"]) == windowstate.MIN_SIZE


@pytest.mark.parametrize(
    "x,y",
    [(-32000, -32000), (5000, 100), (100, 5000), (-3000, 100), (100, -500)],
)
def test_window_state_off_screen_position_is_reset(x, y):
    s = windowstate.sanitize({"width": 1400, "height": 800, "x": x, "y": y}, SCREEN)
    assert "x" not in s and "y" not in s
    assert s["width"] == 1400


def test_window_state_garbage_is_ignored(tmp_path):
    f = tmp_path / "window.json"
    f.write_text("not json")
    assert windowstate.load(f, SCREEN)["width"] == windowstate.DEFAULT_SIZE[0]
    bad = windowstate.sanitize({"width": "a", "height": [1], "x": True}, SCREEN)
    assert bad["maximized"] is False
    assert windowstate.sanitize([1, 2], SCREEN)["height"] == windowstate.DEFAULT_SIZE[1]


def test_window_state_save_failure_is_swallowed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    windowstate.save(blocker / "sub" / "w.json", {"width": 1})  # no raise


def _dummy_spawn(code):
    real = services.spawn

    def spawn(name, args, cwd, env, log_path, job):
        return real(name, [sys.executable, "-c", code], cwd, env, log_path, job)

    return spawn


def test_stack_analysis_failure_names_part_and_keeps_log(tmp_path, monkeypatch):
    monkeypatch.setattr(
        services, "spawn", _dummy_spawn("print('boom on start'); raise SystemExit(3)")
    )
    st = runner.Stack(None, tmp_path, 18763, 18764, logs=tmp_path / "logs", health_timeout=20)
    with pytest.raises(services.StartFailure) as ei:
        st.start()
    assert ei.value.part == "Analysis service did not start"
    assert st.gateway is None
    assert "boom on start" in services.tail_lines(ei.value.log_path)
    st.stop()


def test_stack_gateway_failure_stops_analysis(tmp_path, monkeypatch):
    calls = []
    real = services.spawn

    def spawn(name, args, cwd, env, log_path, job):
        calls.append(name)
        code = "import time;time.sleep(60)" if name == "analysis" else "raise SystemExit(2)"
        return real(name, [sys.executable, "-c", code], cwd, env, log_path, job)

    def healthy(url, child, timeout=0, **kw):
        return child.name == "analysis"

    monkeypatch.setattr(services, "spawn", spawn)
    monkeypatch.setattr(services, "wait_healthy", healthy)
    st = runner.Stack(None, tmp_path, 18765, 18766, logs=tmp_path / "logs", health_timeout=5)
    analysis = None
    try:
        with pytest.raises(services.StartFailure) as ei:
            st.start()
        analysis = st.analysis
        assert ei.value.part == "Gateway did not start"
        assert calls == ["analysis", "gateway"]
    finally:
        st.stop()
    assert st.analysis is None
    assert analysis is not None and not analysis.alive()


def test_stack_env_and_commands(tmp_path, monkeypatch):
    seen = {}

    def spawn(name, args, cwd, env, log_path, job):
        seen[name] = (args, cwd, env)
        raise services.StartFailure("stop here", None)

    monkeypatch.setattr(services, "spawn", spawn)
    (tmp_path / "tools" / "package").mkdir(parents=True)
    st = runner.Stack(None, tmp_path / "w", 18767, 18768, root=tmp_path, logs=tmp_path / "l")
    with pytest.raises(services.StartFailure):
        st.start()
    args, cwd, env = seen["analysis"]
    assert args[1:] == ["-m", "analysis", "--port", "18768"]
    assert cwd == tmp_path and env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_child_python_prefers_console_python(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_bytes(b"")
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert services.child_python() == str(tmp_path / "python.exe")


def test_main_web_not_built(monkeypatch):
    shown = []
    monkeypatch.setattr(paths, "web_dir", lambda *a: None)
    monkeypatch.setattr(ui, "error_window", lambda *a, **k: shown.append(a[0]))
    assert entry.main([]) == 1
    assert shown == ["The web app is not built"]


def test_main_second_instance_focuses_and_exits_0(monkeypatch):
    held = Mutex(entry.MUTEX_NAME)
    focused = []
    monkeypatch.setattr(entry.winjob, "focus_window", lambda t: focused.append(t) or True)
    started = []
    monkeypatch.setattr(entry, "Stack", lambda *a, **k: started.append(1))
    try:
        assert entry.main([]) == 0
    finally:
        held.close()
    assert focused == ["PW Mount Control"] and not started


def test_main_start_failure_shows_error_and_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "web_dir", lambda *a: tmp_path)
    monkeypatch.setattr(services, "spawn", _dummy_spawn("raise SystemExit(1)"))
    shown = []
    monkeypatch.setattr(ui, "error_window", lambda *a, **k: shown.append(a))
    assert entry.main(["--gateway-port", "18769", "--analysis-port", "18762"]) == 1
    assert shown[0][0] == "Analysis service did not start"
    assert shown[0][2] == paths.log_dir()


class FakeStack:
    url = "http://127.0.0.1:1/"
    stopped = 0

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        FakeStack.stopped += 1


def test_main_browser_mode_runs_keepalive_then_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "web_dir", lambda *a: tmp_path)
    monkeypatch.setattr(entry, "Stack", FakeStack)
    opened = []
    monkeypatch.setattr(ui, "run_browser", lambda url, note="": opened.append(url))
    monkeypatch.setattr(ui, "run_window", lambda *a: pytest.fail("no window in browser mode"))
    FakeStack.stopped = 0
    assert entry.main(["--browser"]) == 0
    assert opened == ["http://127.0.0.1:1/"] and FakeStack.stopped >= 1


def test_main_webview_missing_falls_back_to_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "web_dir", lambda *a: tmp_path)
    monkeypatch.setattr(entry, "Stack", FakeStack)

    def boom(*a):
        raise ui.WebViewUnavailable("no runtime")

    notes = []
    monkeypatch.setattr(ui, "run_window", boom)
    monkeypatch.setattr(ui, "run_browser", lambda url, note="": notes.append(note))
    assert entry.main([]) == 0
    assert "WebView2" in notes[0]
