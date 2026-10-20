"""Supervisor PWI4-SIM driver for gate checks (refuses unless PWI4.cfg has Simulator.Mount).

Usage: python tools/pwi4_harness/simdrv.py <cmd> [args]
  connect | goto <alt> <az> [wait] | track_radec <ra_h> <dec_deg> | disable0 | enable0
  | stop | disconnect
Prints a JSON status summary after the command. Used by the T1.4 gate to drive states
(FAULT, sun zone, TRACKING) while the browser gate captures the UI.

Note (BUILD_PLAN §8 P14): PWI4 accepts below-horizon targets and drives the mount to its
mechanical limit, so only use targets that are above the horizon at the current LMST.
"""

import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools" / "pwi4_harness"))
sys.path.insert(0, str(_ROOT / "pwi4"))

from sim_guard import assert_simulator  # noqa: E402

assert_simulator()

from pwi4_client import PWI4  # noqa: E402


def _wait_slew(p: PWI4) -> None:
    time.sleep(0.5)
    while p.status().mount.is_slewing:
        time.sleep(0.2)


def main() -> None:
    p = PWI4()
    cmd, *a = sys.argv[1:]
    if cmd == "connect":
        p.mount_connect()
        p.mount_enable(0)
        p.mount_enable(1)
    elif cmd == "goto":
        p.mount_goto_alt_az(float(a[0]), float(a[1]))
        if len(a) > 2 and a[2] == "wait":
            _wait_slew(p)
    elif cmd == "track_radec":
        p.mount_goto_ra_dec_j2000(float(a[0]), float(a[1]))
        _wait_slew(p)
    elif cmd == "disable0":
        p.mount_disable(0)
    elif cmd == "enable0":
        p.mount_enable(0)
    elif cmd == "stop":
        p.mount_stop()
    elif cmd == "disconnect":
        p.mount_stop()
        p.mount_tracking_off()
        p.mount_disable(0)
        p.mount_disable(1)
        p.mount_disconnect()
    else:
        raise SystemExit(f"unknown command: {cmd}")

    s = p.status()
    m = s.mount
    print(json.dumps({
        "connected": m.is_connected, "az": m.azimuth_degs, "alt": m.altitude_degs,
        "ra_j2000_h": m.ra_j2000_hours, "dec_j2000": m.dec_j2000_degs,
        "slewing": m.is_slewing, "tracking": m.is_tracking, "jd": m.julian_date,
        "lmst": s.site.lmst_hours, "ax_en": [m.axis0.is_enabled, m.axis1.is_enabled],
        "rms": [m.axis0.rms_error_arcsec, m.axis1.rms_error_arcsec],
        "sun_dist": m.distance_to_sun_degs,
    }))


if __name__ == "__main__":
    main()
