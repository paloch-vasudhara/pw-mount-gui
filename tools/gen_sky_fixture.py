r"""One-off: generate web/tests/fixtures/sky-altaz.json (T2.1).

Expected topocentric apparent alt/az (no refraction) from skyfield for five bright stars at a
fixed UTC + site. The star J2000 RA/Dec come from the same pipeline as stars.json
(tools/build_star_data.parse_line: Hipparcos proper motion applied to J2000), rounded like
stars.json. Used by web/tests/unit/sky-chart.spec.ts (tolerance 0.05 deg).

    .venv\Scripts\python tools/gen_sky_fixture.py
"""

from __future__ import annotations

import json
from pathlib import Path

import build_star_data as bsd  # same directory
from skyfield.api import Star, load, wgs84

ROOT = Path(__file__).resolve().parents[1]
SITE = {"lat": 14.3479, "lon": 101.4639, "height_m": 12.0}
UTCS = [(2026, 10, 8, 13, 42, 52), (2026, 10, 8, 19, 30, 0), (2026, 1, 15, 22, 0, 0)]
HIPS = {"Polaris": 11767, "Vega": 91262, "Sirius": 32349, "Canopus": 30438, "Altair": 97649}


def main() -> None:
    want = {str(v): k for k, v in HIPS.items()}
    found: dict[str, tuple[float, float, float]] = {}
    with (ROOT / "data" / "hip_main.txt").open(encoding="ascii", errors="replace") as fh:
        for line in fh:
            hip = line.split("|")[1].strip() if line.count("|") > 2 else ""
            if hip in want:
                p = bsd.parse_line(line)
                assert p is not None
                found[want[hip]] = p
    ts = load.timescale()
    eph = load(str(ROOT / "data" / "ephemeris" / "de421.bsp"))
    obs = eph["earth"] + wgs84.latlon(SITE["lat"], SITE["lon"], elevation_m=SITE["height_m"])
    cases = []
    for utc in UTCS:
        t = ts.utc(*utc)
        stars = []
        for name in HIPS:
            ra, dec, v = found[name]
            ra, dec = round(ra, 4), round(dec, 4)
            s = Star(ra_hours=ra / 15.0, dec_degrees=dec)
            alt, az, _ = obs.at(t).observe(s).apparent().altaz()
            stars.append(
                {
                    "name": name,
                    "hip": HIPS[name],
                    "ra_deg": ra,
                    "dec_deg": dec,
                    "vmag": v,
                    "alt": round(alt.degrees, 5),
                    "az": round(az.degrees, 5),
                }
            )
        cases.append(
            {
                "utc": t.utc_datetime().isoformat(),
                "epoch_ms": int(t.utc_datetime().timestamp() * 1000),
                "stars": stars,
            }
        )
    out = {
        "generated_by": "tools/gen_sky_fixture.py (skyfield de421, topocentric apparent, no refr.)",
        "site": SITE,
        "cases": cases,
    }
    dest = ROOT / "web" / "tests" / "fixtures" / "sky-altaz.json"
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
