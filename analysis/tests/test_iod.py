# ruff: noqa: E501
"""IOD engine + POST /api/iod (T5.3a). Synthetic observations from known TLEs, no network / DB."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from skyfield.api import EarthSatellite, wgs84

from analysis import iod, state
from analysis.app import create_app
from analysis.tests.fixture_catalog import EPOCH_DT, build_sqlite, factory_for, make_tle  # noqa: F401

SITE = {"lat": 13.12, "lon": 100.92, "alt_m": 12.0}
T0 = EPOCH_DT.timestamp()  # fixture TLE epoch, 2026-10-21 00:00 UTC

# (label, TLE, spacing s, expected method, scan step s)
LEO = make_tle(70100, 51.6, 120.0, 0.0005, 90.0, 10.0, 15.2, bstar=" 00000-0")  # ~500 km
LEO_POLAR = make_tle(70101, 97.4, 200.0, 0.0010, 45.0, 120.0, 15.2, bstar=" 00000-0")
MEO = make_tle(70102, 55.0, 10.0, 0.001, 45.0, 300.0, 2.0056, bstar=" 00000-0")
GEO = make_tle(70103, 0.05, 80.0, 0.0002, 0.0, 49.5, 1.00271, bstar=" 00000-0")

CASES = [
    ("LEO 30s", LEO, 30.0, "gauss+herrick-gibbs"),
    ("LEO 60s", LEO, 60.0, "gauss+gibbs"),  # 60 s of a 500 km orbit is 3.8 deg > the 3 deg of O6
    ("LEO 120s", LEO, 120.0, "gauss+gibbs"),
    ("LEO 300s", LEO, 300.0, "gauss+gibbs"),
    ("LEO polar 60s", LEO_POLAR, 60.0, "gauss+gibbs"),
    ("MEO 20min", MEO, 1200.0, "gauss+gibbs"),
    ("GEO 2h", GEO, 7200.0, "gauss+gibbs"),
]


@pytest.fixture(scope="module")
def ts():
    state.warm()
    return state._ts


def _t(ts, t_unix):
    return ts.from_datetime(datetime.fromtimestamp(t_unix, tz=UTC))


def _best_mid(ts, tle, spacing):
    """A mid epoch (unix) a few hours after the TLE epoch when the object is up at the site."""
    sat = EarthSatellite(tle[0], tle[1], "x", ts)
    topo = wgs84.latlon(SITE["lat"], SITE["lon"], SITE["alt_m"])
    t = T0 + 6 * 3600 + np.arange(0, 20 * 3600, 20.0)
    dts = [datetime.fromtimestamp(float(x), UTC) for x in t]
    alt = (sat - topo).at(ts.from_datetimes(dts)).altaz()[0].degrees
    ok = np.nonzero(alt > 30)[0]
    return float(t[ok[0]] if len(ok) else t[int(np.argmax(alt))]) + spacing


def _radec(ts, sat, t_unix):
    topo = wgs84.latlon(SITE["lat"], SITE["lon"], SITE["alt_m"])
    ra, dec, _ = (sat - topo).at(_t(ts, t_unix)).radec()
    return ra.hours * 15.0, dec.degrees


def _kepler_radec(ts, sat, t2, t):
    """Topocentric J2000 RA/Dec of the pure two-body continuation of the t2 state (no J2)."""
    g = sat.at(_t(ts, t2))
    r0, v0 = np.array(g.position.km), np.array(g.velocity.km_per_s)
    f, gg = iod.fg_universal(r0, v0, t - t2)
    d = f * r0 + gg * v0 - iod.site_gcrs_km(SITE["lat"], SITE["lon"], SITE["alt_m"], t)
    d /= np.linalg.norm(d)
    return math.degrees(math.atan2(d[1], d[0])) % 360.0, math.degrees(math.asin(d[2]))


def _obs(ts, tle, t2, spacing, noise_arcsec=0.0, seed=1, truth="sgp4"):
    sat = EarthSatellite(tle[0], tle[1], "x", ts)
    rng = np.random.default_rng(seed)
    out = []
    for t in (t2 - spacing, t2, t2 + spacing):
        ra, dec = _kepler_radec(ts, sat, t2, t) if truth == "kep" else _radec(ts, sat, t)
        if noise_arcsec:
            dec += rng.normal(0, noise_arcsec) / 3600.0
            ra += rng.normal(0, noise_arcsec) / 3600.0 / max(math.cos(math.radians(dec)), 1e-3)
        out.append({"t_unix": t, "ra_deg": ra % 360.0, "dec_deg": dec})
    return out, sat


def _truth(ts, sat, t2):
    g = sat.at(_t(ts, t2))
    return np.array(g.position.km), np.array(g.velocity.km_per_s)


def _angsep(a, b):
    d = (a[0] - b[0] + 180) % 360 - 180
    return math.hypot(d * math.cos(math.radians(b[1])), a[1] - b[1])


def _run(ts, tle, spacing, noise=0.0, truth="sgp4"):
    t2 = _best_mid(ts, tle, spacing)
    obs, sat = _obs(ts, tle, t2, spacing, noise, truth=truth)
    t = time.perf_counter()
    res = iod.solve(SITE, obs)
    dt = time.perf_counter() - t
    r_t, v_t = _truth(ts, sat, t2)
    dr = float(np.linalg.norm(np.array(res["r_km"]) - r_t))
    dv = float(np.linalg.norm(np.array(res["v_kms"]) - v_t)) * 1000.0
    tle_sat = EarthSatellite(res["tle"]["line1"], res["tle"]["line2"], "iod", ts)
    ang = [
        _angsep(_radec(ts, tle_sat, o["t_unix"]), (o["ra_deg"], o["dec_deg"]))
        for o in (obs[0], obs[2])
    ]
    return res, dr, dv, dt, ang, t2


@pytest.mark.parametrize("truth", ["kep", "sgp4"])
@pytest.mark.parametrize("label,tle,spacing,method", CASES, ids=[c[0] for c in CASES])
def test_noise_free(ts, label, tle, spacing, method, truth):
    # truth "kep": two-body continuation of the skyfield t2 state -> the spec criteria, tight.
    # truth "sgp4": skyfield SGP4 positions (J2 etc. that a Kepler fit cannot absorb) -> the
    # state at t2 is only recovered to the J2 level on short LEO arcs (about 1-2 km, 10-20 m/s).
    res, dr, dv, dt, ang, t2 = _run(ts, tle, spacing, truth=truth)
    f = res["tle_fit"]
    print(
        f"IOD {truth} {label}: |dr2| {dr:.3f} km, |dv2| {dv:.3f} m/s, {res['method']}, "
        f"iter {res['iterations']}, TLE fit {f['pos_err_km'] * 1000:.1f} m / {f['vel_err_ms']:.4f} m/s "
        f"({f['iterations']} it, conv {f['converged']}), TLE RA/Dec err t1/t3 "
        f"{ang[0]:.4f}/{ang[1]:.4f} deg, {dt * 1000:.0f} ms, roots {res['roots']['count']}"
    )
    assert res["method"] == method
    if truth == "kep" or "LEO" not in label:
        assert dr <= 1.0 and dv <= 1.0
    else:
        assert dr <= 3.0 and dv <= 25.0
    assert res["epoch_unix"] == pytest.approx(t2)
    assert f["converged"] is True
    tol = 0.010 if "LEO" in label else 0.150  # TLE field resolution at MEO / GEO range
    assert f["pos_err_km"] <= tol
    assert f["vel_err_ms"] <= 0.05
    if "LEO" in label:
        assert max(ang) <= (0.05 if truth == "sgp4" else 0.1)
    assert dt < 0.5


def test_coe_and_tle_format(ts):
    res, *_ = _run(ts, LEO, 60.0)
    assert res["coe"]["period_min"] == pytest.approx(94.7, abs=1.0)
    assert res["coe"]["i_deg"] == pytest.approx(51.6, abs=0.2)
    assert res["coe"]["a_km"] == pytest.approx(6878, abs=15)
    l1, l2 = res["tle"]["line1"], res["tle"]["line2"]
    assert len(l1) == len(l2) == 69
    assert l1.startswith("1 A9999U ") and l2.startswith("2 A9999 ")
    for line in (l1, l2):
        assert int(line[68]) == iod._checksum(line[:68])
    assert l1[9:17] == " " * 8  # blank international designator
    assert "00000-0" in l1[53:61]  # B* = 0


def test_unsorted_obs_sorted_by_engine(ts):
    t2 = _best_mid(ts, LEO, 120.0)
    obs, sat = _obs(ts, LEO, t2, 120.0)
    res = iod.solve(SITE, [obs[2], obs[0], obs[1]])
    assert res["epoch_unix"] == pytest.approx(t2)
    r_t, _ = _truth(ts, sat, t2)
    assert np.linalg.norm(np.array(res["r_km"]) - r_t) <= 1.0


def test_two_arcsec_noise_logged(ts):
    for label, tle, spacing in (("LEO 120s", LEO, 120.0), ("LEO 300s", LEO, 300.0), ("GEO 2h", GEO, 7200.0)):
        res, dr, dv, dt, ang, _ = _run(ts, tle, spacing, noise=2.0)
        print(
            f"IOD 2arcsec noise {label}: |dr2| {dr:.2f} km, |dv2| {dv:.1f} m/s, {res['method']}, "
            f"a {res['coe']['a_km']:.0f} km e {res['coe']['e']:.4f}, TLE fit "
            f"{res['tle_fit']['pos_err_km'] * 1000:.1f} m conv {res['tle_fit']['converged']}"
        )
        assert math.isfinite(dr)


# ------------------------------------------------------------------ failure branches
def _obs_at(ra_dec, t0=T0 + 7200.0, step=60.0):
    return [{"t_unix": t0 + i * step, "ra_deg": a, "dec_deg": d} for i, (a, d) in enumerate(ra_dec)]


def test_fail_count_and_duplicates():
    good = _obs_at([(10, 5), (20, 8), (30, 12)])
    with pytest.raises(iod.IodError) as e:
        iod.solve(SITE, good[:2])
    assert e.value.message == iod.MSG_THREE
    dup = [dict(good[0]), dict(good[1]), dict(good[1])]
    with pytest.raises(iod.IodError) as e:
        iod.solve(SITE, dup)
    assert e.value.message == iod.MSG_THREE
    with pytest.raises(iod.IodError):
        iod.solve(SITE, good + good[:1])


def test_fail_degenerate_geometry():
    same = _obs_at([(50, 20), (50, 20), (50, 20)])
    with pytest.raises(iod.IodError) as e:
        iod.solve(SITE, same)
    assert e.value.message == iod.MSG_DEGENERATE
    line = _obs_at([(10, 0), (20, 0), (30, 0)])  # three directions on one great circle, evenly
    with pytest.raises(iod.IodError) as e:
        iod.solve(SITE, line)
    assert e.value.message == iod.MSG_DEGENERATE


def test_fail_no_root_and_unbound():
    # Implausible geometry searches: every random draw must end in a readable IodError, and the
    # three distinct messages below must all be reachable.
    rng = np.random.default_rng(7)
    seen = set()
    for _ in range(400):
        pts = [(float(rng.uniform(0, 360)), float(rng.uniform(-60, 60))) for _ in range(3)]
        step = float(rng.choice([5.0, 60.0, 600.0, 3600.0]))
        try:
            iod.solve(SITE, _obs_at(pts, step=step))
        except iod.IodError as e:
            seen.add(e.message)
    print("IOD failure messages reached by random geometry:", sorted(seen))
    assert iod.MSG_NOROOT in seen
    assert seen <= {
        iod.MSG_NOROOT,
        iod.MSG_DIVERGED,
        iod.MSG_UNBOUND,
        iod.MSG_DEGENERATE,
        iod.MSG_THREE,
    }


def test_fail_unbound_on_fast_motion(ts):
    # LEO observations with spacing stretched past an orbit: the solution cannot be a sane orbit
    t2 = _best_mid(ts, LEO, 60.0)
    obs, _ = _obs(ts, LEO, t2, 60.0)
    for o, dt in zip(obs, (-9000.0, 0.0, 9000.0), strict=True):
        o["t_unix"] = t2 + dt
    try:
        iod.solve(SITE, obs)
    except iod.IodError as e:
        assert e.message in {
            iod.MSG_NOROOT,
            iod.MSG_UNBOUND,
            iod.MSG_DIVERGED,
            iod.MSG_DEGENERATE,
        }


def test_unbound_and_diverged_helpers():
    r = np.array([7000.0, 0.0, 0.0])
    with pytest.raises(iod.IodError) as e:  # hyperbolic
        iod.coe_from_rv(r, np.array([0.0, 12.0, 0.0]))
    assert e.value.message == iod.MSG_UNBOUND
    with pytest.raises(iod.IodError) as e:  # perigee below the surface
        iod.coe_from_rv(r, np.array([0.0, 5.0, 0.0]))
    assert e.value.message == iod.MSG_UNBOUND
    ok = iod.coe_from_rv(r, np.array([0.0, math.sqrt(iod.MU / 7000.0), 0.0]))
    assert ok["e"] == pytest.approx(0, abs=1e-9) and ok["a_km"] == pytest.approx(7000.0)
    with pytest.raises(iod.IodError) as e:  # the f / g solver refuses NaN states
        iod.fg_universal(r, np.array([float("nan"), 0.0, 0.0]), 60.0)
    assert e.value.message == iod.MSG_DIVERGED


def test_tle_fit_falls_back_to_osculating(ts, monkeypatch):
    t2 = _best_mid(ts, LEO, 120.0)
    obs, _ = _obs(ts, LEO, t2, 120.0)
    monkeypatch.setattr(iod, "_sgp4_state", lambda x, d: None)  # SGP4 rejects every element set
    res = iod.solve(SITE, obs)
    assert res["tle_fit"]["converged"] is False
    assert len(res["tle"]["line1"]) == len(res["tle"]["line2"]) == 69
    assert math.isfinite(res["r_km"][0])


def test_herrick_gibbs_vs_gibbs_switch(ts):
    # O6 boundary: 30 s (1.9 deg) -> Herrick-Gibbs, 60 s (3.8 deg) -> Gibbs on the same orbit
    a = iod.solve(SITE, _obs(ts, LEO, _best_mid(ts, LEO, 30.0), 30.0)[0])
    b = iod.solve(SITE, _obs(ts, LEO, _best_mid(ts, LEO, 60.0), 60.0)[0])
    assert (a["method"], b["method"]) == ("gauss+herrick-gibbs", "gauss+gibbs")


# ------------------------------------------------------------------ endpoint
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = tmp_path_factory.mktemp("iod") / "cat.db"
    build_sqlite(path)
    with TestClient(create_app(factory_for(path))) as c:
        yield c


def test_endpoint_ok(ts, client):
    t2 = _best_mid(ts, LEO, 120.0)
    obs, sat = _obs(ts, LEO, t2, 120.0)
    r = client.post("/api/iod", json={"site": SITE, "obs": obs})
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) >= {"epoch_unix", "r_km", "v_kms", "method", "iterations", "coe", "tle", "tle_fit"}
    assert set(j["coe"]) == {"a_km", "e", "i_deg", "raan_deg", "argp_deg", "nu_deg", "period_min"}
    assert set(j["tle"]) == {"line1", "line2"}
    assert set(j["tle_fit"]) >= {"iterations", "pos_err_km", "vel_err_ms"}
    r_t, _ = _truth(ts, sat, t2)
    assert np.linalg.norm(np.array(j["r_km"]) - r_t) <= 1.0


def test_endpoint_failures(client):
    good = _obs_at([(10, 5), (20, 8), (30, 12)])
    r = client.post("/api/iod", json={"site": SITE, "obs": good[:2]})
    assert r.status_code == 422 and r.json()["detail"] == iod.MSG_THREE
    same = _obs_at([(50, 20)] * 3)
    r = client.post("/api/iod", json={"site": SITE, "obs": same})
    assert r.status_code == 422 and r.json()["detail"] == iod.MSG_DEGENERATE
    r = client.post("/api/iod", json={"site": SITE})  # malformed body: pydantic 422
    assert r.status_code == 422


def test_endpoint_hides_unexpected_errors(client, monkeypatch):
    def boom(site, obs):
        raise RuntimeError("secret internals dsn=postgres://u:p@h/db")

    monkeypatch.setattr(iod, "solve", boom)
    good = _obs_at([(10, 5), (20, 8), (30, 12)])
    r = client.post("/api/iod", json={"site": SITE, "obs": good})
    assert r.status_code == 503
    assert "secret" not in r.text and "postgres" not in r.text


def test_endpoint_timeout(client, monkeypatch):
    from analysis import app as app_mod

    monkeypatch.setattr(app_mod, "IOD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(iod, "solve", lambda site, obs: time.sleep(0.5))
    good = _obs_at([(10, 5), (20, 8), (30, 12)])
    r = client.post("/api/iod", json={"site": SITE, "obs": good})
    assert r.status_code == 503 and "timed out" in r.json()["detail"]


_ = timedelta  # keep import for readability of future cases


def test_vallado_example_7_2_angles_only():
    """Vallado, Fundamentals of Astrodynamics and Applications (4th ed.), Example 7-2 (pp. 447-449):
    obs 3, 5, 6 from 40 N, 110 W, 2000 m on 2012-08-20, against the book's known orbit at 11:48:28.
    (The book's own Gauss result is a = 12 641.6 km; its EOP corrections are not applied here.)"""
    import calendar
    import datetime as dt

    def u(h, m, s):
        return calendar.timegm(dt.datetime(2012, 8, 20, h, m, s).timetuple())

    obs = [
        {"t_unix": u(11, 40, 28), "ra_deg": 0.939913, "dec_deg": 18.667717},
        {"t_unix": u(11, 48, 28), "ra_deg": 45.025748, "dec_deg": 35.664741},
        {"t_unix": u(11, 52, 28), "ra_deg": 67.886655, "dec_deg": 36.996583},
    ]
    r = iod.solve({"lat": 40.0, "lon": -110.0, "alt_m": 2000.0}, obs)
    truth_r = np.array([6356.486034, 5290.532258, 6511.396979])
    truth_v = np.array([-4.172948, 4.776550, 1.720271])
    assert r["method"] == "gauss+gibbs"  # the book: vectors 17 deg / 8 deg apart -> Gibbs
    assert np.linalg.norm(np.array(r["r_km"]) - truth_r) < 1.0
    assert np.linalg.norm(np.array(r["v_kms"]) - truth_v) * 1000 < 1.0
    c = r["coe"]
    assert abs(c["a_km"] - 12246.023) < 10.0
    assert abs(c["e"] - 0.2) < 0.001
    assert abs(c["i_deg"] - 40.0) < 0.01
    assert abs(c["raan_deg"] - 330.0) < 0.01
