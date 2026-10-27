"""Sun / Moon topocentric positions via skyfield (offline, local de421.bsp)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .transforms import refract_alt

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
        # T3.3b (P18): the Sun is in the mount's frame (observed = refracted like PWI4), so its
        # distance to the reported alt/az and the safety comparisons are consistent. The Moon
        # stays refraction-free (display only).
        s_alt_obs = refract_alt(float(s_alt.degrees))
        illum = float(almanac.fraction_illuminated(self._eph, "moon", t))
        self._cache = SunMoon(
            sun_alt=s_alt_obs,
            sun_az=float(s_az.degrees),
            moon_alt=float(m_alt.degrees),
            moon_az=float(m_az.degrees),
            moon_illumination=illum,
        )
        self._cache_key = site
        self._cache_t = time.monotonic()
        return self._cache

    def _observer(self, site: tuple[float, float, float]):
        from skyfield.api import wgs84

        lat, lon, h = site
        return self._eph["earth"] + wgs84.latlon(lat, lon, elevation_m=h)

    def sun_altaz(
        self,
        site: tuple[float, float, float],
        when: datetime | None = None,
        *,
        unrefracted: bool = False,
    ) -> tuple[float, float]:
        """Observed Sun (alt, az) in degrees for `site` (safety maths): refracted like the
        mount's reported alt/az (T3.3b, P18); `unrefracted=True` for the bare apparent position."""
        t = self._ts.from_datetime(when or datetime.now(UTC))
        alt, az, _ = self._observer(site).at(t).observe(self._eph["sun"]).apparent().altaz()
        a = float(alt.degrees)
        return (a if unrefracted else refract_alt(a)), float(az.degrees)

    def radec_j2000_altaz(
        self,
        site: tuple[float, float, float],
        ra_h: float,
        dec_deg: float,
        when: datetime | None = None,
        *,
        unrefracted: bool = False,
    ) -> tuple[float, float]:
        """Observed (alt, az) of a J2000 RA/Dec at `when`: precession, nutation, aberration and
        refraction like PWI4 (`unrefracted=True`: no refraction). H4, T3.3 / T3.3b."""
        from .transforms import radec_j2000_to_altaz

        return radec_j2000_to_altaz(
            ra_h,
            dec_deg,
            when or datetime.now(UTC),
            site,
            ts=self._ts,
            earth=self._eph["earth"],
            unrefracted=unrefracted,
        )

    def altaz_radec_j2000(
        self,
        site: tuple[float, float, float],
        alt: float,
        az: float,
        when: datetime,
        *,
        unrefracted: bool = False,
    ) -> tuple[float, float]:
        """Inverse of `radec_j2000_altaz` (H4): (alt, az) is refracted unless `unrefracted`."""
        from .transforms import altaz_to_radec_j2000

        return altaz_to_radec_j2000(
            alt, az, when, site, ts=self._ts, earth=self._eph["earth"], unrefracted=unrefracted
        )
