"""H4 (T3.3): J2000 RA/Dec <-> topocentric alt/az with an EXPLICIT time.

Convention (P17, same as PWI4): the alt/az side is the *apparent* topocentric direction with
precession, nutation, annual aberration and light deflection, and NO atmospheric refraction.
`t_utc` is always passed in (no "now" inside); the site is `(lat_deg, lon_deg_east, height_m)`.

The inverse undoes aberration by iterating on the apparent -> astrometric offset (a plain
`from_altaz().radec()` leaves ~20" of aberration); it converges to < 1e-9" in a few passes.

Earth-orientation data: skyfield's built-in UT1-UTC / delta-T tables, no polar motion (<= 0.3").
The ephemeris and timescale are the ones the gateway already loaded (`Ephem`); nothing is
reloaded per call.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

Site = tuple[float, float, float]

MAX_ITER = 8
CONVERGED_RAD = 1e-12  # ~2e-7 arcsec


def _vec(ra_rad: float, dec_rad: float) -> list[float]:
    cd = math.cos(dec_rad)
    return [cd * math.cos(ra_rad), cd * math.sin(ra_rad), math.sin(dec_rad)]


def _unit(v) -> list[float]:
    n = math.sqrt(float(v[0]) ** 2 + float(v[1]) ** 2 + float(v[2]) ** 2)
    return [float(v[0]) / n, float(v[1]) / n, float(v[2]) / n]


def _aware(t_utc: datetime) -> datetime:
    if t_utc.tzinfo is None:
        raise ValueError("t_utc must be timezone-aware (UTC)")
    return t_utc.astimezone(UTC)


def _observer(earth: Any, site: Site):
    from skyfield.api import wgs84

    lat, lon, h = site
    return earth + wgs84.latlon(lat, lon, elevation_m=h)


def _apparent_dir(obs_at: Any, ra_h: float, dec_deg: float) -> list[float]:
    """Apparent direction (unit vector in the GCRS axes) of a J2000/ICRS RA/Dec."""
    from skyfield.api import Star

    star = Star(ra_hours=ra_h, dec_degrees=dec_deg)
    return _unit(obs_at.observe(star).apparent().position.au)


def radec_j2000_to_altaz(
    ra_h: float, dec_deg: float, t_utc: datetime, site: Site, *, ts: Any, earth: Any
) -> tuple[float, float]:
    """Apparent topocentric (alt, az) in degrees of a J2000 RA/Dec (no refraction)."""
    from skyfield.api import Star

    t = ts.from_datetime(_aware(t_utc))
    star = Star(ra_hours=ra_h, dec_degrees=dec_deg)
    alt, az, _ = _observer(earth, site).at(t).observe(star).apparent().altaz()
    return float(alt.degrees), float(az.degrees)


def altaz_to_radec_j2000(
    alt: float, az: float, t_utc: datetime, site: Site, *, ts: Any, earth: Any
) -> tuple[float, float]:
    """J2000 (ra_h in [0, 24), dec_deg) whose apparent topocentric position is (alt, az)."""
    t = ts.from_datetime(_aware(t_utc))
    at = _observer(earth, site).at(t)
    # The observed (apparent) direction in the GCRS axes.
    target = _unit(at.from_altaz(alt_degrees=alt, az_degrees=az).position.au)
    ast = list(target)  # first guess: astrometric = apparent
    for _ in range(MAX_ITER):
        ra = math.atan2(ast[1], ast[0]) % math.tau
        dec = math.asin(max(-1.0, min(1.0, ast[2])))
        app = _apparent_dir(at, ra * 12.0 / math.pi, math.degrees(dec))
        # shift of the guess so that its apparent direction moves onto the target
        step = [target[i] - app[i] for i in range(3)]
        ast = _unit([ast[i] + step[i] for i in range(3)])
        if max(abs(s) for s in step) < CONVERGED_RAD:
            break
    ra = math.atan2(ast[1], ast[0]) % math.tau
    dec = math.asin(max(-1.0, min(1.0, ast[2])))
    return (ra * 12.0 / math.pi) % 24.0, math.degrees(dec)


# ------------------------------------------------------------------ POST /api/transform schema

# Imported here (not at the top) only to keep the pure maths above free of web concerns.
from typing import Annotated, Literal  # noqa: E402

from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

MAX_T_YEARS = 50.0
Num = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AltAzValues(_Strict):
    alt: Annotated[Num, Field(ge=-90.0, le=90.0)]
    az: Annotated[Num, Field(ge=0.0, lt=360.0)]


class RaDecValues(_Strict):
    ra_h: Annotated[Num, Field(ge=0.0, lt=24.0)]
    dec_deg: Annotated[Num, Field(ge=-90.0, le=90.0)]


class SiteValues(_Strict):
    lat: Annotated[Num, Field(ge=-90.0, le=90.0)]
    lon: Annotated[Num, Field(ge=-180.0, le=180.0)]
    height_m: Annotated[Num, Field(ge=-500.0, le=10000.0)]


class TransformRequest(_Strict):
    dir: Literal["altaz_to_radec", "radec_to_altaz"]
    t_utc: Annotated[str, Field(min_length=10, max_length=40)]
    values: AltAzValues | RaDecValues
    site: SiteValues | None = None

    @model_validator(mode="after")
    def _check(self) -> TransformRequest:
        want = AltAzValues if self.dir == "altaz_to_radec" else RaDecValues
        if not isinstance(self.values, want):
            raise ValueError("values do not match dir")
        self.time()  # validates the timestamp
        return self

    def time(self) -> datetime:
        try:
            t = datetime.fromisoformat(self.t_utc)
        except ValueError:
            raise ValueError("t_utc is not an ISO 8601 timestamp") from None
        if t.tzinfo is None:
            raise ValueError("t_utc needs a timezone (use Z for UTC)")
        t = t.astimezone(UTC)
        if abs((t - datetime.now(UTC)).total_seconds()) > MAX_T_YEARS * 365.25 * 86400.0:
            raise ValueError("t_utc is more than 50 years from now")
        return t
