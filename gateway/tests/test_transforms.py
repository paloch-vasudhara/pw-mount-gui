"""H4 (T3.3): J2000 <-> alt/az transforms vs astropy (dev-only reference), round trip, endpoint.

astropy is a DEV dependency only; no runtime code imports it. Earth-orientation choice for the
comparison: skyfield's own UT1-UTC (`t.dut1`) is handed to astropy (so a stale IERS table cannot
contaminate the result and no download is needed); polar motion is whatever astropy's bundled
IERS-B knows (<= 0.3 arcsec, zero outside its range) while H4 uses none. Refraction is off
(pressure 0) in astropy for the geometry comparison (H4 `unrefracted=True`): precession, nutation,
aberration, light deflection, sidereal time.

T3.3b (P18): the DEFAULT H4 frame is refracted like PWI4. That model is PWI4's own, fitted on the
simulator (Saemundsson x 0.9865), not astropy's; it is checked against (a) the table of values
measured on the PWI4 simulator, (b) astropy's ERFA refraction at PWI4's pressure/temperature
defaults (loose: another model), (c) the exact round trip.
"""

from __future__ import annotations

import math
import time
import warnings
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.ephem import Ephem
from gateway.transforms import (
    altaz_to_radec_j2000,
    radec_j2000_to_altaz,
    refract_alt,
    refraction_arcsec,
    unrefract_alt,
)

from .conftest import FakePwi4

astropy = pytest.importorskip("astropy")

import astropy.units as u  # noqa: E402
from astropy.coordinates import AltAz, EarthLocation, SkyCoord  # noqa: E402
from astropy.time import Time  # noqa: E402
from astropy.utils import iers  # noqa: E402

iers.conf.auto_download = False  # offline-safe: never fetch IERS data
iers.conf.iers_degraded_accuracy = "ignore"  # dates outside the bundled table: no warning/error

TOKEN = "t" * 43
SITES = [
    (-60.0, -70.0, 2400.0),
    (-30.0, 150.0, 0.0),
    (0.0, 10.0, 500.0),
    (14.3479, 100.5656, 12.0),
    (70.0, -150.0, 300.0),
]
YEARS = [2000, 2009, 2018, 2026, 2040]
ALTS = [5.0, 30.0, 60.0, 89.0]
AZS = [float(a) for a in range(0, 360, 30)] + [359.9]
ARCSEC = 3600.0


@pytest.fixture(scope="module")
def ephem() -> Ephem:
    e = Ephem(Settings().ephemeris)
    assert e.available, e.error
    return e


def sep_arcsec(a1_deg: float, d1_deg: float, a2_deg: float, d2_deg: float) -> float:
    a1, d1, a2, d2 = map(math.radians, (a1_deg, d1_deg, a2_deg, d2_deg))
    # haversine form: accurate at sub-arcsecond separations
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((a2 - a1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h)))) * ARCSEC


def _frame(ephem: Ephem, t: datetime, site) -> AltAz:
    at = Time(t, scale="utc")
    at.delta_ut1_utc = float(ephem._ts.from_datetime(t).dut1)
    loc = EarthLocation.from_geodetic(site[1] * u.deg, site[0] * u.deg, site[2] * u.m)
    return AltAz(obstime=at, location=loc, pressure=0 * u.hPa)


def test_h4_vs_astropy_and_round_trip(ephem):
    worst_vs, worst_rt, n = 0.0, 0.0, 0
    ts, earth = ephem._ts, ephem._eph["earth"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # erfa 'dubious year' for 2040 UTC
        for si, site in enumerate(SITES):
            for yi, year in enumerate(YEARS):
                month, day = 1 + (si * 3 + yi) % 12, 1 + (si + 2 * yi) % 27
                t = datetime(year, month, day, 3 + si * 4, 17, 41, tzinfo=UTC)
                frame = _frame(ephem, t, site)
                altaz = [(a, z) for a in ALTS for z in AZS]
                # altaz -> radec
                ours = [
                    altaz_to_radec_j2000(a, z, t, site, ts=ts, earth=earth, unrefracted=True)
                    for a, z in altaz
                ]
                ref = SkyCoord(
                    alt=[a for a, _ in altaz] * u.deg, az=[z for _, z in altaz] * u.deg, frame=frame
                ).transform_to("icrs")
                for (ra_h, dec), r_ra, r_dec in zip(ours, ref.ra.deg, ref.dec.deg, strict=True):
                    worst_vs = max(worst_vs, sep_arcsec(ra_h * 15.0, dec, r_ra, r_dec))
                # radec -> altaz (the same directions as J2000 inputs), and the round trip
                ref2 = SkyCoord(
                    ra=[r * 15.0 for r, _ in ours] * u.deg,
                    dec=[d for _, d in ours] * u.deg,
                    frame="icrs",
                ).transform_to(frame)
                for (ra_h, dec), (a, z), r_alt, r_az in zip(
                    ours, altaz, ref2.alt.deg, ref2.az.deg, strict=True
                ):
                    alt2, az2 = radec_j2000_to_altaz(
                        ra_h, dec, t, site, ts=ts, earth=earth, unrefracted=True
                    )
                    worst_vs = max(worst_vs, sep_arcsec(az2, alt2, r_az, r_alt))
                    worst_rt = max(worst_rt, sep_arcsec(az2, alt2, z, a))
                    n += 1
    print(
        f"\nH4 vs astropy: max {worst_vs:.3f} arcsec; round trip max {worst_rt:.4f} arcsec; n={n}"
    )
    assert worst_vs <= 1.0
    assert worst_rt <= 0.1


def test_inverse_undoes_aberration(ephem):
    """A naive from_altaz().radec() is several arcsec off (aberration); H4 is not."""
    from skyfield.api import wgs84

    ts, earth = ephem._ts, ephem._eph["earth"]
    t = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    site = (14.3479, 100.5656, 12.0)
    at = (earth + wgs84.latlon(site[0], site[1], elevation_m=site[2])).at(ts.from_datetime(t))
    worst_naive = 0.0
    for alt, az in [(45.0, 100.0), (30.0, 250.0), (70.0, 10.0), (20.0, 180.0), (60.0, 300.0)]:
        ra_n, dec_n, _ = at.from_altaz(alt_degrees=alt, az_degrees=az).radec()
        ra_h, dec = altaz_to_radec_j2000(alt, az, t, site, ts=ts, earth=earth, unrefracted=True)
        worst_naive = max(worst_naive, sep_arcsec(ra_h * 15.0, dec, ra_n._degrees, dec_n.degrees))
        back = radec_j2000_to_altaz(ra_h, dec, t, site, ts=ts, earth=earth, unrefracted=True)
        assert sep_arcsec(back[1], back[0], az, alt) < 0.1
    assert worst_naive > 5.0, worst_naive


# PWI4 simulator measurement (T3.3b, P18): (true altitude deg, PWI4 refraction arcsec) from
# reported alt/az vs H4-unrefracted of PWI4's own J2000 RA/Dec (time offset removed, see
# test_track_pwi4sim). Subset of the 82 measured poses (3.6 .. 89 deg, 7 azimuths).
PWI4_REFRACTION = [
    (3.645, 716.00), (4.567, 612.36), (5.017, 571.05), (6.051, 493.52), (7.010, 437.34),
    (8.987, 352.49), (11.010, 292.78), (13.980, 232.95), (15.943, 204.76), (19.955, 162.54),
    (24.965, 127.50), (29.971, 103.40), (39.980, 71.40), (49.986, 50.15), (59.990, 34.65),
    (69.994, 21.69), (79.997, 10.55), (85.999, 4.15),
]  # fmt: skip


def test_refraction_matches_pwi4_measurement():
    worst = max(abs(refraction_arcsec(h) - r) for h, r in PWI4_REFRACTION)
    print(f"\nrefraction model vs PWI4 table: max {worst:.2f} arcsec (n={len(PWI4_REFRACTION)})")
    assert worst <= 1.5
    # shape: monotonic decreasing, never negative, zero far below the horizon
    prev = math.inf
    for h in [x * 0.25 for x in range(-4, 360)]:
        r = refraction_arcsec(h)
        assert 0.0 <= r <= prev + 1e-9, (h, r)
        prev = r
    assert refraction_arcsec(-2.0) == 0.0 and refraction_arcsec(-30.0) == 0.0
    assert refraction_arcsec(90.0) == 0.0


def test_refraction_continuous_and_exactly_invertible():
    # continuity through the -1..-2 deg taper and monotonic observed altitude
    prev = -math.inf
    for i in range(-3000, 9001):
        h = i / 100.0
        o = refract_alt(h)
        assert o > prev, h
        prev = o
        assert abs(unrefract_alt(o) - h) * 3600.0 < 1e-5, h
    assert abs(refract_alt(-1.0001) - refract_alt(-0.9999)) < 0.001


def test_refracted_default_vs_astropy_refraction(ephem):
    """Another model (ERFA refco), only a loose cross-check at PWI4's pressure / temperature
    defaults (1018 hPa, 16 C): within 12 arcsec above 15 deg altitude."""
    ts, earth = ephem._ts, ephem._eph["earth"]
    site = (14.3479, 100.5656, 12.0)
    t = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    at = Time(t, scale="utc")
    at.delta_ut1_utc = float(ts.from_datetime(t).dut1)
    loc = EarthLocation.from_geodetic(site[1] * u.deg, site[0] * u.deg, site[2] * u.m)
    frame = AltAz(
        obstime=at, location=loc, pressure=1018 * u.hPa, temperature=16 * u.deg_C,
        relative_humidity=0.0, obswl=0.55 * u.micron,
    )  # fmt: skip
    worst = 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for alt in [15.0, 20.0, 30.0, 45.0, 60.0, 75.0]:
            for az in [10.0, 130.0, 250.0]:
                ra_h, dec = altaz_to_radec_j2000(alt, az, t, site, ts=ts, earth=earth)
                ref = SkyCoord(ra=ra_h * 15.0 * u.deg, dec=dec * u.deg, frame="icrs")
                r = ref.transform_to(frame)
                worst = max(worst, sep_arcsec(az, alt, r.az.deg, r.alt.deg))
    print(f"\nH4 refracted vs astropy refraction (1018 hPa/16 C): max {worst:.2f} arcsec")
    assert worst <= 12.0


def test_refracted_round_trip(ephem):
    ts, earth = ephem._ts, ephem._eph["earth"]
    site = (-30.0, 150.0, 300.0)
    worst = 0.0
    for _k, t in enumerate(
        [datetime(2026, 3, 3, 1, tzinfo=UTC), datetime(2026, 10, 20, 15, 30, tzinfo=UTC)]
    ):
        for alt in [-0.5, 0.0, 3.0, 5.0, 12.0, 30.0, 60.0, 85.0, 89.9]:
            for az in [0.0, 77.0, 180.0, 291.0, 359.9]:
                ra_h, dec = altaz_to_radec_j2000(alt, az, t, site, ts=ts, earth=earth)
                a2, z2 = radec_j2000_to_altaz(ra_h, dec, t, site, ts=ts, earth=earth)
                worst = max(worst, sep_arcsec(z2, a2, az, alt))
    print(f"\nrefracted round trip: max {worst:.5f} arcsec")
    assert worst <= 0.1


def test_refracted_is_higher_than_unrefracted(ephem):
    ts, earth = ephem._ts, ephem._eph["earth"]
    t = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    site = (14.3479, 100.5656, 12.0)
    ra_h, dec = altaz_to_radec_j2000(20.0, 100.0, t, site, ts=ts, earth=earth, unrefracted=True)
    ar = radec_j2000_to_altaz(ra_h, dec, t, site, ts=ts, earth=earth)
    au = radec_j2000_to_altaz(ra_h, dec, t, site, ts=ts, earth=earth, unrefracted=True)
    assert abs(au[0] - 20.0) < 1e-6 and au[1] == pytest.approx(ar[1], abs=1e-9)
    assert 150.0 < (ar[0] - au[0]) * 3600.0 < 175.0  # ~2.7 arcmin at 20 deg


def test_naive_datetime_rejected(ephem):
    with pytest.raises(ValueError):
        radec_j2000_to_altaz(
            1.0, 2.0, datetime(2026, 1, 1), (0, 0, 0), ts=ephem._ts, earth=ephem._eph["earth"]
        )


def test_ephem_wrapper_matches_h4(ephem):
    t = datetime(2026, 3, 3, 12, tzinfo=UTC)
    site = (14.3479, 100.5656, 12.0)
    a1 = ephem.radec_j2000_altaz(site, 5.5, 20.0, t)
    a2 = radec_j2000_to_altaz(5.5, 20.0, t, site, ts=ephem._ts, earth=ephem._eph["earth"])
    assert a1 == a2
    ra, dec = ephem.altaz_radec_j2000(site, a1[0], a1[1], t)
    assert sep_arcsec(ra * 15.0, dec, 5.5 * 15.0, 20.0) < 0.1


# ------------------------------------------------------------------ endpoint

H = {"x-mount-token": TOKEN, "origin": "http://127.0.0.1:3200"}
BODY = {
    "dir": "altaz_to_radec",
    "t_utc": "2026-10-22T12:00:00Z",
    "values": {"alt": 45.0, "az": 120.0},
    "site": {"lat": 14.3479, "lon": 100.5656, "height_m": 12.0},
}


def _client(down: bool = False):
    fake = FakePwi4()
    fake.down = down
    app = create_app(Settings(port=8400, token=TOKEN), transport=fake.transport)
    return app, TestClient(app, base_url="http://127.0.0.1:8400")


def test_transform_requires_token_and_origin():
    app, tc = _client()
    with tc:
        assert tc.post("/api/transform", json=BODY).status_code == 401
        bad_token = {"x-mount-token": "x" * 43}
        assert tc.post("/api/transform", json=BODY, headers=bad_token).status_code == 401
        bad_origin = {"x-mount-token": TOKEN, "origin": "http://evil.example"}
        assert tc.post("/api/transform", json=BODY, headers=bad_origin).status_code == 403


def test_transform_result_matches_h4_and_round_trips():
    app, tc = _client()
    with tc:
        r = tc.post("/api/transform", json=BODY, headers=H)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["dir"] == "altaz_to_radec" and out["t_utc"] == "2026-10-22T12:00:00.000Z"
        assert out["frame"] == "observed (refracted like PWI4)"
        ra_h, dec = out["result"]["ra_h"], out["result"]["dec_deg"]
        t = datetime(2026, 10, 22, 12, tzinfo=UTC)
        site = (14.3479, 100.5656, 12.0)
        assert (ra_h, dec) == app.state.ephem.altaz_radec_j2000(site, 45.0, 120.0, t)
        back = tc.post(
            "/api/transform",
            json={
                "dir": "radec_to_altaz",
                "t_utc": "2026-10-22T14:00:00+02:00",  # same instant, other offset
                "values": {"ra_h": ra_h, "dec_deg": dec},
                "site": BODY["site"],
            },
            headers=H,
        ).json()["result"]
        assert sep_arcsec(back["az"], back["alt"], 120.0, 45.0) < 0.1


def test_transform_default_site_is_pwi4_site():
    app, tc = _client()
    with tc:
        end = time.monotonic() + 5
        while app.state.poller.pwi4_site() is None and time.monotonic() < end:
            time.sleep(0.02)
        body = {k: v for k, v in BODY.items() if k != "site"}
        r = tc.post("/api/transform", json=body, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["site"] == {"lat": 14.3479, "lon": 100.5656, "height_m": 12.0}


def test_transform_503_without_site():
    app, tc = _client(down=True)
    with tc:
        body = {k: v for k, v in BODY.items() if k != "site"}
        assert tc.post("/api/transform", json=body, headers=H).status_code == 503


def _with(**over):
    b = dict(BODY)
    b.update(over)
    return b


@pytest.mark.parametrize(
    "body",
    [
        _with(dir="nope"),
        _with(values={"alt": 91.0, "az": 10.0}),
        _with(values={"alt": -91.0, "az": 10.0}),
        _with(values={"alt": 10.0, "az": 360.0}),
        _with(values={"alt": 10.0, "az": -0.1}),
        _with(values={"ra_h": 1.0, "dec_deg": 2.0}),  # does not match dir
        _with(dir="radec_to_altaz", values={"alt": 10.0, "az": 10.0}),
        _with(dir="radec_to_altaz", values={"ra_h": 24.0, "dec_deg": 0.0}),
        _with(dir="radec_to_altaz", values={"ra_h": 1.0, "dec_deg": 90.5}),
        _with(values={"alt": 10.0, "az": 10.0, "x": 1}),
        _with(values={"alt": "10", "az": 10.0}),
        _with(values={"alt": True, "az": 10.0}),
        _with(t_utc="2026-10-22T12:00:00"),  # no timezone
        _with(t_utc="not a time"),
        _with(t_utc="1970-01-01T00:00:00Z"),
        _with(t_utc="2090-01-01T00:00:00Z"),
        _with(site={"lat": 91.0, "lon": 0.0, "height_m": 0.0}),
        _with(site={"lat": 10.0, "lon": 181.0, "height_m": 0.0}),
        _with(extra=1),
        {"dir": "altaz_to_radec"},
    ],
)
def test_transform_validation(body):
    app, tc = _client()
    with tc:
        assert tc.post("/api/transform", json=body, headers=H).status_code == 422


def test_transform_takes_no_control_lock():
    app, tc = _client()
    with tc:
        assert tc.post("/api/transform", json=BODY, headers=H).status_code == 200
        assert app.state.control.holder is None
