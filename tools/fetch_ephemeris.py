"""Download the JPL DE421 ephemeris into data/ephemeris/ (git-ignored) for offline use."""

from __future__ import annotations

import sys
from pathlib import Path

from skyfield.api import Loader

DEST = Path(__file__).resolve().parents[1] / "data" / "ephemeris"
NAME = "de421.bsp"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    target = DEST / NAME
    if target.exists():
        print(f"already present: {target} ({target.stat().st_size} bytes)")
        return 0
    Loader(str(DEST))(NAME)
    print(f"downloaded: {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
