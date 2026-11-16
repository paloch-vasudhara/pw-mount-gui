"""Child services: ports, start, health wait, graceful stop, MediaMTX decision, logs."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .winjob import Job

log = logging.getLogger("launcher")

GATEWAY_PORTS = (8400, 8401, 8419)  # default, range start, range end
ANALYSIS_PORTS = (8410, 8411, 8429)
MEDIAMTX_HLS_PORT = 8888
HEALTH_TIMEOUT_S = 60.0
HEALTH_POLL_S = 0.5
GATEWAY_STOP_S = 10.0
ANALYSIS_STOP_S = 5.0
MEDIAMTX_STOP_S = 3.0
LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 4  # launcher.log + 4 rotated = 5 files

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Run in a throw-away helper: attach to the child's (hidden) console and send it CTRL_BREAK.
# Out of process so the launcher's own console state is never touched.
_BREAK_SNIPPET = (
    "import ctypes,sys\n"
    "k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
    "pid=int(sys.argv[1])\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(pid): sys.exit(2)\n"
    "k.SetConsoleCtrlHandler(None,True)\n"
    "sys.exit(0 if k.GenerateConsoleCtrlEvent(1,pid) else 3)\n"
)


# ---------------------------------------------------------------- logging


def setup_logging(directory: Path, console: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "launcher.log"
    root = logging.getLogger("launcher")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(logging.INFO)
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return path


def rotate_log(path: Path, backups: int = LOG_BACKUPS) -> None:
    """Keep the previous runs: x.log -> x.log.1 -> ... -> x.log.<backups>."""
    try:
        for i in range(backups, 0, -1):
            src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
            dst = path.with_name(f"{path.name}.{i}")
            if src.exists():
                os.replace(src, dst)
    except OSError as exc:
        log.warning("log rotation failed for %s: %s", path.name, exc)


def tail_lines(path: Path | None, n: int = 30) -> str:
    if path is None:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(deque(f, maxlen=n))
    except OSError:
        return ""


# ---------------------------------------------------------------- ports


def port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(default: int, lo: int, hi: int, taken: tuple[int, ...] = ()) -> int | None:
    """default if free, else the next free port in lo..hi (never one of `taken`)."""
    for p in (default, *range(lo, hi + 1)):
        if p not in taken and port_free(p):
            return p
    return None


def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- children


def child_python() -> str:
    """The console python.exe (children run with CREATE_NO_WINDOW), never pythonw.exe."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.is_file():
            return str(alt)
    return str(exe)


@dataclass
class Child:
    name: str
    proc: subprocess.Popen
    log_path: Path
    _fh: object = field(default=None, repr=False)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None


def spawn(
    name: str,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    job: Job | None,
) -> Child:
    rotate_log(log_path)
    fh = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
    except Exception:
        fh.close()
        raise
    child = Child(name, proc, log_path, fh)
    if job is not None:
        try:
            job.assign(proc.pid)
        except OSError as exc:
            log.warning("%s: could not join the job object (%s)", name, exc)
    log.info("%s started (pid %d)", name, proc.pid)
    return child


def send_ctrl_break(pid: int) -> bool:
    try:
        r = subprocess.run(
            [child_python(), "-c", _BREAK_SNIPPET, str(pid)],
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return r.returncode == 0
    except Exception as exc:
        log.warning("CTRL_BREAK helper failed: %s", exc)
        return False


def stop_child(child: Child, timeout: float, graceful: bool = True) -> str:
    """CTRL_BREAK (so uvicorn runs its lifespan shutdown), wait, then terminate, then kill."""
    how = "already exited"
    if child.alive():
        how = "terminated"
        if graceful and send_ctrl_break(child.pid):
            try:
                child.proc.wait(timeout=timeout)
                how = "graceful"
            except subprocess.TimeoutExpired:
                how = "terminated"
        if child.alive():
            child.proc.terminate()
            try:
                child.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.proc.kill()
                child.proc.wait(timeout=3)
    if child._fh is not None:
        try:
            child._fh.close()
        except OSError:
            pass
    log.info("%s stopped (%s)", child.name, how)
    return how


# ---------------------------------------------------------------- health


class StartFailure(Exception):
    def __init__(self, part: str, log_path: Path | None) -> None:
        super().__init__(part)
        self.part = part
        self.log_path = log_path


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_healthy(
    url: str,
    child: Child | None,
    timeout: float = HEALTH_TIMEOUT_S,
    poll: float = HEALTH_POLL_S,
    check: Callable[[str], bool] = http_ok,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once `url` answers 200; False on timeout or when the child exited early."""
    end = clock() + timeout
    while True:
        if child is not None and not child.alive():
            return False
        if check(url):
            return True
        if clock() >= end:
            return False
        sleep(poll)


# ---------------------------------------------------------------- MediaMTX


def cctv_hls_enabled(settings_file: Path) -> bool:
    """Read-only: the station settings have CCTV enabled with type HLS. Content never logged."""
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        c = data.get("cctv") or {}
        return bool(c.get("enabled")) and c.get("type") == "hls"
    except Exception:
        return False


def mediamtx_decision(
    settings_file: Path,
    folder: Path | None,
    listening: Callable[[int], bool] = port_listening,
) -> tuple[bool, str]:
    if not cctv_hls_enabled(settings_file):
        return False, "CCTV (HLS) not enabled"
    if folder is None:
        return False, "no mediamtx.exe + mediamtx.yml found"
    if listening(MEDIAMTX_HLS_PORT):
        return False, f"127.0.0.1:{MEDIAMTX_HLS_PORT} already in use (an existing MediaMTX)"
    return True, "start"
