"""Safety guard: refuse to talk to PWI4 unless its active mount device is the simulator.

Every harness entry point must call :func:`assert_simulator` before sending any command.
This module only ever *reads* PWI4.cfg; it never writes any PWI4 settings file.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8220
SIM_MOUNT = "Simulator.Mount"


class GuardError(RuntimeError):
    """Raised when it is not provably safe (simulator only) to command PWI4."""


def default_cfg_path() -> Path:
    env = os.environ.get("PWI4_CFG")
    if env:
        return Path(env)
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Documents" / "PlaneWave Instruments" / "PWI4" / "Settings" / "PWI4.cfg"


def read_mount_device(cfg_path: Path) -> str:
    """Return the value of ``Devices.Mount`` from a PWI4.cfg file (read-only)."""
    try:
        text = Path(cfg_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GuardError(f"cannot read PWI4 config {cfg_path}: {exc}") from exc
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "Devices.Mount":
            return value.strip()
    raise GuardError(f"Devices.Mount not found in {cfg_path}")


def pwi4_reachable(host: str = HOST, port: int = PORT, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def assert_simulator(cfg_path: Path | str | None = None, *, check_reachable: bool = True) -> str:
    """Raise :class:`GuardError` unless PWI4 is configured for the simulator mount.

    Returns the config path string that was validated. Sends no command to PWI4.
    """
    path = Path(cfg_path) if cfg_path else default_cfg_path()
    device = read_mount_device(path)
    if device != SIM_MOUNT:
        raise GuardError(
            f"REFUSING: Devices.Mount = {device!r} in {path}; only {SIM_MOUNT!r} is allowed"
        )
    if check_reachable and not pwi4_reachable():
        raise GuardError(f"PWI4 is not reachable on {HOST}:{PORT}")
    return str(path)
