"""Uninstaller helper: `python -m launcher.cleanup --all` removes this Windows user's stored
logins (Credential Manager service `pw-mount-control`) and the data folder
`%LOCALAPPDATA%/pw-mount-control` (settings, window position, logs).

Prints only counts, never a value. Entries of other services are never touched.
"""

from __future__ import annotations

import argparse
import shutil
import sys

from analysis.db import KEYRING_SERVICE, KEYRING_USER
from gateway.settings import ADSB_KEYRING_USER, CCTV_KEYRING_USER
from gateway.settings import KEYRING_USER as ASTROMETRY_KEYRING_USER

from . import paths

KEYRING_USERS = (KEYRING_USER, ASTROMETRY_KEYRING_USER, CCTV_KEYRING_USER, ADSB_KEYRING_USER)


def remove_logins() -> int:
    """Delete our entries; the number removed (a missing entry or backend is not an error)."""
    import keyring

    n = 0
    for user in KEYRING_USERS:
        try:
            if keyring.get_password(KEYRING_SERVICE, user) is None:
                continue
            keyring.delete_password(KEYRING_SERVICE, user)
            n += 1
        except Exception:  # noqa: BLE001 - no backend / already gone
            continue
    return n


def remove_data() -> bool:
    d = paths.data_dir()
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="launcher.cleanup", description=__doc__.split("\n")[0])
    parser.add_argument("--all", action="store_true", required=True, help="logins and data folder")
    parser.parse_args(argv)
    logins = remove_logins()
    data = remove_data()
    print(f"removed {logins} stored login(s); data folder {'removed' if data else 'not present'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
