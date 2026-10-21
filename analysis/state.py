"""Single-object state for the Track Info card / hover tooltip (T2.3).

One skyfield `EarthSatellite` per call, in the API process (it must answer in well under 20 ms):
geometric az/alt/range for a site (same convention as the chunks: no refraction) and the sunlit
flag via `is_sunlit` with the local DE421 ephemeris. No database access here; the caller supplies
the TLE row.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from skyfield.api import EarthSatellite, load, load_file, wgs84

REPO_ROOT = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_ts = None
_eph = None


def ephemeris_path() -> Path:
    return Path(os.environ.get("PW_EPHEMERIS") or REPO_ROOT / "data" / "ephemeris" / "de421.bsp")


def warm() -> None:
    """Load the timescale + ephemeris once (called at start-up, in a worker thread)."""
    global _ts, _eph
    with _lock:
        if _ts is None:
            _ts = load.timescale()
        if _eph is None:
            _eph = load_file(str(ephemeris_path()))


def _finite(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


def compute_state(row: dict, lat: float, lon: float, alt_m: float, t_unix: float) -> dict:
    """`row` is a catalogue `get()` row (info fields + tle_line1/2)."""
    warm()
    # Unix time has no leap seconds: go through a UTC datetime. (`ts.utc(1970, 1, 1, 0, 0, t_unix)`
    # counts elapsed SI seconds incl. the 27 leap seconds since 1972 -> 27 s behind; T2.3 gate.)
    t = _ts.from_datetime(datetime.fromtimestamp(t_unix, tz=timezone.utc))  # type: ignore[union-attr]
    sat = EarthSatellite(row["tle_line1"], row["tle_line2"], row["name"] or "", _ts)
    topo = (sat - wgs84.latlon(lat, lon, alt_m)).at(t)
    alt, az, dist = topo.altaz()
    az_v, alt_v, rng = _finite(az.degrees), _finite(alt.degrees), _finite(dist.km)
    lit: bool | None = None
    if az_v is not None:
        try:
            lit = bool(sat.at(t).is_sunlit(_eph))
        except Exception:
            lit = None
    return {
        "norad": row["norad"],
        "name": row["name"],
        "cospar": row["cospar"],
        "country": row["country"],
        "object_type": row["object_type"],
        "orbit_class": row["orbit_class"],
        "az": az_v,
        "alt": alt_v,
        "range_km": rng,
        "illuminated": lit,
        "tle_epoch": row["tle_epoch"],
        "tle_age_days": _finite(float(t.tt - sat.epoch.tt)),
    }
