"""PWI4-sim scenarios. Skipped unless PWI4_SIM=1 (and the guard passes)."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

NAMES = [
    "01_connect_enable_home",
    "02_goto_altaz",
    "03_stop_midslew",
    "04_radec_tracking",
    "05_follow_tle",
    "06_custom_path",
    "07_offsets_jog_step",
    "08_watchdog",
    "09_status_latency",
    "10_disconnect",
]


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    import run_harness

    return run_harness.run_all(out_dir=tmp_path_factory.mktemp("pwi4_sim"))


@pytest.mark.parametrize("name", NAMES)
def test_scenario_ok(report, name):
    entry = report["scenarios"][name]
    assert entry["status"] == "ok", entry.get("traceback")


def test_mount_left_disconnected(report):
    assert report["final_mount_is_connected"] is False
