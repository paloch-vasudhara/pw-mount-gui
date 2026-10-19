import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sim_guard  # noqa: E402


def _cfg(tmp_path, device):
    p = tmp_path / "PWI4.cfg"
    p.write_text(f"Foo.Bar = 1\nDevices.Mount                       = {device}\nX = y\n")
    return p


def test_simulator_passes(tmp_path):
    p = _cfg(tmp_path, "Simulator.Mount")
    assert sim_guard.assert_simulator(p, check_reachable=False) == str(p)


def test_real_mount_refused(tmp_path):
    with pytest.raises(sim_guard.GuardError, match="REFUSING"):
        sim_guard.assert_simulator(_cfg(tmp_path, "Elmo.L350.Mount"), check_reachable=False)


def test_missing_file_refused(tmp_path):
    with pytest.raises(sim_guard.GuardError):
        sim_guard.assert_simulator(tmp_path / "nope.cfg", check_reachable=False)


def test_missing_key_refused(tmp_path):
    p = tmp_path / "PWI4.cfg"
    p.write_text("Other = 1\n")
    with pytest.raises(sim_guard.GuardError):
        sim_guard.assert_simulator(p, check_reachable=False)


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PWI4_CFG", str(_cfg(tmp_path, "Elmo.L350.Mount")))
    with pytest.raises(sim_guard.GuardError):
        sim_guard.assert_simulator(check_reachable=False)
    monkeypatch.setenv("PWI4_CFG", str(_cfg(tmp_path, "Simulator.Mount")))
    assert sim_guard.assert_simulator(check_reachable=False)


def test_unreachable_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sim_guard, "pwi4_reachable", lambda *a, **k: False)
    with pytest.raises(sim_guard.GuardError, match="not reachable"):
        sim_guard.assert_simulator(_cfg(tmp_path, "Simulator.Mount"))
