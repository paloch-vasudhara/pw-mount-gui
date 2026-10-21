r"""Build web/public/data/stars.json from the Hipparcos main catalogue (T2.1, O5).

    .venv\Scripts\python tools/build_star_data.py [--vmag 6.5] [--src data/hip_main.txt]

hip_main.txt is pipe-separated (0-based split indices): 1 HIP, 5 Vmag, 8 RAdeg, 9 DEdeg
(ICRS, epoch J1991.25), 12 pmRA*cos(dec) mas/yr, 13 pmDE mas/yr. Positions are moved
linearly to J2000.0 with the proper motion. Output stars: [ra_deg, dec_deg, vmag],
brightest first.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HIP_EPOCH = 1991.25
J2000 = 2000.0


def parse_line(line: str) -> tuple[float, float, float] | None:
    """One catalogue line -> (ra_deg_J2000, dec_deg_J2000, vmag), or None if unusable."""
    f = line.split("|")
    if len(f) < 14:
        return None
    try:
        vmag = float(f[5])
        ra = float(f[8])
        dec = float(f[9])
    except ValueError:  # blank Vmag / position
        return None
    try:
        pm_ra = float(f[12])
        pm_de = float(f[13])
    except ValueError:
        pm_ra = pm_de = 0.0
    dt = J2000 - HIP_EPOCH
    cosd = max(math.cos(math.radians(dec)), 1e-6)
    ra = (ra + pm_ra / cosd * dt / 3.6e6) % 360.0
    dec = max(-90.0, min(90.0, dec + pm_de * dt / 3.6e6))
    return ra, dec, vmag


def build(lines, vmag_max: float) -> list[list[float]]:
    out = []
    for line in lines:
        p = parse_line(line)
        if p is None or p[2] > vmag_max:
            continue
        out.append([round(p[0], 4), round(p[1], 4), round(p[2], 2)])
    out.sort(key=lambda s: s[2])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vmag", type=float, default=6.5)
    ap.add_argument("--src", type=Path, default=ROOT / "data" / "hip_main.txt")
    ap.add_argument("--out", type=Path, default=ROOT / "web" / "public" / "data" / "stars.json")
    a = ap.parse_args()
    with a.src.open(encoding="ascii", errors="replace") as fh:
        stars = build(fh, a.vmag)
    doc = {
        "source": "Hipparcos (ESA 1997)",
        "vmag_max": a.vmag,
        "epoch": "J2000",
        "count": len(stars),
        "stars": stars,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{len(stars)} stars (V <= {a.vmag}) -> {a.out}")


if __name__ == "__main__":
    main()
