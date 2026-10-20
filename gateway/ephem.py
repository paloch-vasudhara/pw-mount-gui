"""Sun / Moon topocentric positions via skyfield (offline, local de421.bsp)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REFRESH_S = 1.0


@dataclass
class SunMoon:
    sun_alt: float
    sun_az: float
    moon_alt: float
    moon_az: float
    moon_illumination: float


def angular_sep_deg(az1: float, alt1: float, az2: float, alt2: float) -> float:
    """Great-circle separation between two alt/az directions (degrees)."""
    a1, h1, a2, h2 = map(math.radians, (az1, alt1, az2, alt2))
    c = math.sin(h1) * math.sin(h2) + math.cos(h1) * math.cos(h2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


class Ephem:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.available = False
        self.error: str | None = None
        self._eph = None
        self._ts = None
        self._cache: SunMoon | None = None
        self._cache_key: tuple[float, float, float] | None = None
        self._cache_t = 0.0
        try:
            from skyfield.api import load, load_file

            self._ts = load.timescale()
            self._eph = load_file(str(self.path))
            self.available = True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def stale(self, site: tuple[float, float, float] | None) -> bool:
        if not self.available or site is None:
            return False
        return (
            self._cache is None
            or self._cache_key != site
            or time.monotonic() - self._cache_t >= REFRESH_S
        )

    @property
    def cached(self) -> SunMoon | None:
        return self._cache

    def update(self, site: tuple[float, float, float], when: datetime | None = None) -> SunMoon:
        """Compute (blocking, a few ms). site = (lat_deg, lon_deg_east, height_m)."""
        from skyfield import almanac
        from skyfield.api import wgs84

        lat, lon, h = site
        when = when or datetime.now(UTC)
        t = self._ts.from_datetime(when)
        earth, sun, moon = self._eph["earth"], self._eph["sun"], self._eph["moon"]
        obs = earth + wgs84.latlon(lat, lon, elevation_m=h)
        s_alt, s_az, _ = obs.at(t).observe(sun).apparent().altaz()
        m_alt, m_az, _ = obs.at(t).observe(moon).apparent().altaz()
        illum = float(almanac.fraction_illuminated(self._eph, "moon", t))
        self._cache = SunMoon(
            sun_alt=float(s_alt.degrees),
            sun_az=float(s_az.degrees),
            moon_alt=float(m_alt.degrees),
            moon_az=float(m_az.degrees),
            moon_illumination=illum,
        )
        self._cache_key = site
        self._cache_t = time.monotonic()
        return self._cache
