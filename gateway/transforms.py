"""H4 (T3.3, refraction added in T3.3b): J2000 RA/Dec <-> topocentric alt/az, EXPLICIT time.

Frame (P17 + P18, same as PWI4): the J2000 side is catalogue coordinates; the alt/az side is the
*observed* topocentric direction = apparent (precession, nutation, annual aberration, light
deflection) PLUS atmospheric refraction, exactly what PWI4 reports. `unrefracted=True` gives /
takes the refraction-free apparent altitude (internal use). Azimuth is never refracted.
`t_utc` is always passed in (no "now" inside); the site is `(lat_deg, lon_deg_east, height_m)`.

Refraction model (P18, measured on the PWI4 simulator, T3.3b): Saemundsson's formula for the
true altitude h (degrees) -> refraction R in arcseconds,
    R = 60 * 1.02 * F / tan(h + 10.3 / (h + 5.11))      [deg inside the tan]
with F = 0.9865 (PWI4's pressure/temperature scale, a constant: equals 1010 hPa / 10 C scaled by
about 1018 hPa / 16 C). Fitted over true altitudes 3.6 .. 89 deg on three azimuth sets: residual
vs PWI4 <= 1" (see test_transforms). No temperature / pressure inputs (decision Q5).
Edges: R is clamped >= 0 (the formula turns slightly negative within 0.11 deg of the zenith) and
is tapered linearly to 0 between h = -1 and h = -2 deg (it diverges at h = -5.11); below -2 deg
there is no refraction. The taper keeps h -> observed altitude continuous and monotonic, so the
exact inverse (Newton on the same function) always exists.

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

TRANSFORM_FRAME = "observed (refracted like PWI4)"  # stated in the /api/transform response

MAX_ITER = 8
CONVERGED_RAD = 1e-12  # ~2e-7 arcsec

REFRACTION_FACTOR = 0.9865  # PWI4's pressure/temperature scale on Saemundsson (fitted, P18)
REFRACTION_TAPER_TOP_DEG = -1.0  # full formula for true altitude >= this
REFRACTION_TAPER_END_DEG = -2.0  # no refraction at or below this


def _saemundsson_arcsec(h_deg: float) -> float:
    arg = math.radians(h_deg + 10.3 / (h_deg + 5.11))
    return max(0.0, 60.0 * 1.02 * REFRACTION_FACTOR / math.tan(arg))


def refraction_arcsec(alt_true_deg: float) -> float:
    """Refraction (arcsec, added to the true altitude) PWI4 applies at a true altitude."""
    h = alt_true_deg
    if h <= REFRACTION_TAPER_END_DEG:
        return 0.0
    if h >= REFRACTION_TAPER_TOP_DEG:
        return _saemundsson_arcsec(h)
    top = _saemundsson_arcsec(REFRACTION_TAPER_TOP_DEG)
    span = REFRACTION_TAPER_TOP_DEG - REFRACTION_TAPER_END_DEG
    return top * (h - REFRACTION_TAPER_END_DEG) / span


def refract_alt(alt_true_deg: float) -> float:
    """Observed (refracted) altitude, degrees, of a true (apparent, refraction-free) altitude."""
    return alt_true_deg + refraction_arcsec(alt_true_deg) / 3600.0


def unrefract_alt(alt_obs_deg: float) -> float:
    """Exact inverse of `refract_alt` (Newton with a numeric slope; < 1e-9 deg)."""
    h = alt_obs_deg
    for _ in range(30):
        f = refract_alt(h) - alt_obs_deg
        if abs(f) < 1e-10:
            break
        d = 1e-4
        slope = (refract_alt(h + d) - refract_alt(h - d)) / (2 * d)
        h -= f / (slope if slope > 0.05 else 1.0)
    return h


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
    ra_h: float,
    dec_deg: float,
    t_utc: datetime,
    site: Site,
    *,
    ts: Any,
    earth: Any,
    unrefracted: bool = False,
) -> tuple[float, float]:
    """Observed topocentric (alt, az) in degrees of a J2000 RA/Dec: refracted like PWI4's
    reported alt/az (`unrefracted=True`: the apparent direction without refraction)."""
    from skyfield.api import Star

    t = ts.from_datetime(_aware(t_utc))
    star = Star(ra_hours=ra_h, dec_degrees=dec_deg)
    alt, az, _ = _observer(earth, site).at(t).observe(star).apparent().altaz()
    alt_deg = float(alt.degrees)
    return (alt_deg if unrefracted else refract_alt(alt_deg)), float(az.degrees)


def altaz_to_radec_j2000(
    alt: float,
    az: float,
    t_utc: datetime,
    site: Site,
    *,
    ts: Any,
    earth: Any,
    unrefracted: bool = False,
) -> tuple[float, float]:
    """J2000 (ra_h in [0, 24), dec_deg) whose observed (refracted, like PWI4) topocentric
    position is (alt, az); `unrefracted=True`: (alt, az) is the refraction-free direction."""
    if not unrefracted:
        alt = unrefract_alt(alt)
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
