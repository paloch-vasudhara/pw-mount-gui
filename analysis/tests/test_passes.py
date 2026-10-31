"""Pass engine + POST /api/passes (T5.1a). Synthetic TLEs, temp SQLite catalogue, no network."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient
from skyfield.api import EarthSatellite, wgs84

from analysis import app as app_mod
from analysis import state
from analysis.app import create_app
from analysis.db import CatalogUnavailable
from analysis.passes import compute_passes
from analysis.tests.fixture_catalog import build_sqlite, factory_for, make_tle

SITE = {"lat": 14.3479, "lon": 100.5656, "alt_m": 12.0}
E = 1793188800.0  # 2026-10-28 12:00 UTC
DAY0 = 1792540800  # 2026-10-21 00:00 UTC (fixture TLE epoch)
# LEO test TLE (copy of gateway/tests/tlegen.PEAK_70): 15 deg crossing ~60 s after E, peak 70 deg
LEO = (
    "1 99001U          26301.50000000  .00000000  00000-0  10000-3 0    03",
    "2 99001  51.6000 304.9966 0005000  90.0000 275.3438 15.50000000    07",
)
MEO = make_tle(70000, 55.0, 0.0, 0.001, 45.0, 300.0, 2.0056)  # two arcs above 30 deg per day
GEO = make_tle(70001, 0.05, 80.0, 0.0002, 0.0, 49.5, 1.00271)  # ~E100, always up at the site


@pytest.fixture(scope="module")
def env():
    state.warm()
    return state._ts, state._eph, wgs84.latlon(SITE["lat"], SITE["lon"], SITE["alt_m"])


def _dt(t):
    return [datetime.fromtimestamp(float(x), UTC) for x in t]


def run(env, tle, lim, t0, t1, mode="los", sun_alt_max=0.0):
    ts, eph, topos = env
    sat = EarthSatellite(tle[0], tle[1], "x", ts)
    return compute_passes(sat, topos, ts, eph, lim, t0, t1, mode, sun_alt_max)


def test_leo_pass_vs_bruteforce(env):
    ts, _, topos = env
    rows = run(env, LEO, 15.0, E, E + 900)
    assert len(rows) == 1
    p = rows[0]
    t = np.arange(E, E + 900, 0.1)  # brute force: 0.1 s skyfield scan
    sat = EarthSatellite(LEO[0], LEO[1], "x", ts)
    alt, az, _ = (sat - topos).at(ts.from_datetimes(_dt(t))).altaz()
    a = alt.degrees
    up = int(np.nonzero((a[:-1] < 15) & (a[1:] >= 15))[0][0])
    dn = int(np.nonzero((a[:-1] >= 15) & (a[1:] < 15))[0][-1])
    d_rise, d_set = abs(p["t_rise"] - t[up]), abs(p["t_set"] - t[dn])
    d_pk = abs(p["max_el"] - a.max())
    d_tpk = abs(p["t_peak"] - t[int(np.argmax(a))])
    print(
        f"LEO accuracy: rise {d_rise:.3f} s, set {d_set:.3f} s, "
        f"peak {d_pk:.5f} deg, t_peak {d_tpk:.3f} s"
    )
    assert d_rise <= 0.5 and d_set <= 0.5
    assert d_pk <= 0.01 and d_tpk <= 0.5
    assert abs(p["rise_az"] - az.degrees[up]) < 0.1 and abs(p["set_az"] - az.degrees[dn]) < 0.1
    assert p["duration_s"] == pytest.approx(p["t_set"] - p["t_rise"])
    assert "visible" not in p


def test_window_clip_and_pass_in_progress(env):
    full = run(env, LEO, 15.0, E, E + 900)[0]
    # Start in the middle of the pass: true rise/set still reported, duration clipped
    mid = full["t_rise"] + 100
    p = run(env, LEO, 15.0, mid, mid + 900)[0]
    assert p["t_rise"] == pytest.approx(full["t_rise"], abs=0.5)
    assert p["duration_s"] == pytest.approx(full["t_set"] - mid, abs=0.5)
    # End inside the pass
    p = run(env, LEO, 15.0, E, full["t_rise"] + 50)[0]
    assert p["duration_s"] == pytest.approx(50, abs=0.5)
    assert p["t_set"] == pytest.approx(full["t_set"], abs=0.5)
    # A window that starts after the pass has set: nothing
    assert run(env, LEO, 15.0, full["t_set"] + 5, full["t_set"] + 60) == []


def test_two_hump_meo(env):
    rows = run(env, MEO, 30.0, DAY0, DAY0 + 86400)
    assert len(rows) == 2
    assert all(r["t_rise"] and r["t_set"] for r in rows)
    assert rows[0]["t_rise"] < rows[1]["t_rise"]
    assert [round(r["max_el"]) for r in rows] == [31, 36]


def test_geo_row_with_nulls(env):
    rows = run(env, GEO, 15.0, DAY0, DAY0 + 6 * 3600)
    assert len(rows) == 1
    r = rows[0]
    assert r["t_rise"] is None and r["t_set"] is None
    assert r["rise_az"] is None and r["set_az"] is None
    assert r["duration_s"] == pytest.approx(6 * 3600)
    assert r["max_el"] > 15


def test_optical_shadowed_dropped_and_twilight_partial(env):
    # LEO passes from E: the one at E+60 enters Earth shadow mid-pass (twilight, partial); the one
    # near E+167132 is in Earth shadow for its whole length.
    shadow = (E + 167000, E + 167500)
    assert len(run(env, LEO, 15.0, *shadow, mode="los")) == 1
    assert run(env, LEO, 15.0, *shadow, mode="optical") == []
    los = run(env, LEO, 15.0, E, E + 600)[0]
    opt = run(env, LEO, 15.0, E, E + 600, mode="optical")
    assert len(opt) == 1
    o = opt[0]
    assert 0 < o["visible_s"] < o["duration_s"]
    assert o["visible_s"] == pytest.approx(sum(b - a for a, b in o["visible"]))
    assert los["t_rise"] == o["t_rise"]
    # sun_alt_max = -90: the Sun is never low enough -> dropped
    assert run(env, LEO, 15.0, E, E + 600, mode="optical", sun_alt_max=-90.0) == []
    # a pass that is lit and dark all through keeps its whole duration
    full = run(env, LEO, 15.0, E + 83000, E + 84000, mode="optical")
    assert len(full) == 1 and full[0]["visible_s"] == pytest.approx(full[0]["duration_s"], abs=1.0)


def test_optical_visible_edge_is_refined(env):
    ts, eph, _ = env
    o = run(env, LEO, 15.0, E, E + 600, mode="optical")[0]
    sat = EarthSatellite(LEO[0], LEO[1], "x", ts)
    t_end = o["visible"][-1][1]  # lit 1 s before the end of the sub-interval, dark 1 s after
    lit = sat.at(ts.from_datetimes(_dt([t_end - 1.0, t_end + 1.0]))).is_sunlit(eph)
    assert bool(lit[0]) and not bool(lit[1])


# ---- endpoint -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("passes") / "cat.db")
    build_sqlite(path)
    with TestClient(create_app(factory_for(path))) as c:
        end = time.time() + 60
        while time.time() < end and not c.get("/api/sats/status").json()["ready"]:
            time.sleep(0.1)
        yield c


def body(**kw):
    b = {
        "norad": 25544,
        "site": SITE,
        "lim_el": 15,
        "start_unix": DAY0,
        "end_unix": DAY0 + 43200,
        "mode": "los",
    }
    b.update(kw)
    return b


def test_endpoint_ok_sorted(client):
    r = client.post("/api/passes", json=body(end_unix=DAY0 + 3 * 86400))
    assert r.status_code == 200
    j = r.json()
    assert j["norad"] == 25544 and j["name"] == "ISS (ZARYA)"
    assert "tle_epoch" in j and "tle_age_days" in j
    ps = j["passes"]
    assert len(ps) >= 2
    assert [p["t_rise"] for p in ps] == sorted(p["t_rise"] for p in ps)
    assert all(15 <= p["max_el"] <= 90 and p["duration_s"] > 0 for p in ps)
    o = client.post("/api/passes", json=body(mode="optical", end_unix=DAY0 + 3 * 86400)).json()[
        "passes"
    ]
    assert len(o) <= len(ps) and all(p["visible_s"] > 0 and p["visible"] for p in o)


def test_endpoint_validation(client):
    assert client.post("/api/passes", json=body(end_unix=DAY0)).status_code == 422
    assert client.post("/api/passes", json=body(end_unix=DAY0 - 5)).status_code == 422
    r = client.post("/api/passes", json=body(end_unix=DAY0 + 7 * 86400 + 1))
    assert r.status_code == 422 and r.json()["detail"] == "Window longer than 7 days"
    assert client.post("/api/passes", json=body(mode="radar")).status_code == 422
    assert client.post("/api/passes", json=body(end_unix=DAY0 + 7 * 86400)).status_code == 200


def test_endpoint_404(client):
    for n in (99999, 11111):  # unknown, decayed
        r = client.post("/api/passes", json=body(norad=n))
        assert r.status_code == 404 and r.json()["detail"] == "not found or decayed"


def test_endpoint_timeout(client, monkeypatch):
    monkeypatch.setattr(app_mod, "PASSES_TIMEOUT_S", 0.001)
    r = client.post("/api/passes", json=body(end_unix=DAY0 + 6 * 86400))
    assert r.status_code == 503 and r.json()["detail"] == "Pass computation timed out"


def test_endpoint_catalogue_error_503():
    def bad():
        raise CatalogUnavailable("satellite database is not configured")

    with TestClient(create_app(bad)) as c:
        r = c.post("/api/passes", json=body())
        assert r.status_code == 503 and "configured" in r.json()["detail"]
