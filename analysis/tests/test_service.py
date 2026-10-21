from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from analysis.app import create_app
from analysis.chunks import decode_chunk
from analysis.db import CatalogUnavailable
from analysis.tests.fixture_catalog import build_sqlite, factory_for

DAY0 = 1792540800
SITE = {"lat": 14.3479, "lon": 100.5656, "alt_m": 12.0}


def unconfigured_factory():
    raise CatalogUnavailable("satellite database is not configured")


def _wait_ready(c: TestClient, timeout: float = 60.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if c.get("/api/sats/status").json()["ready"]:
            return
        time.sleep(0.1)
    raise AssertionError("catalogue never became ready")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("svc") / "cat.db")
    build_sqlite(path)
    with TestClient(create_app(factory_for(path))) as c:
        _wait_ready(c)
        yield c


def test_health_unchanged(client):
    body = {"ok": True, "service": "analysis", "version": "0.1.0"}
    assert client.get("/api/health").json() == body


def test_status_counts(client):
    s = client.get("/api/sats/status").json()
    assert s["ready"] and s["counts"] == {"leo": 2, "meo": 1, "geo": 3}


def test_objects_only_live_payloads(client):
    leo = client.get("/api/sky/objects", params={"layer": "leo"}).json()
    assert {o[0] for o in leo} == {25544, 43013}
    assert leo[0][1] and len(leo[0]) == 5
    geo = client.get("/api/sky/objects", params={"layer": "geo"}).json()
    assert {o[0] for o in geo} == {41000, 41001, 39000}
    assert {o[0] for o in client.get("/api/sky/objects", params={"layer": "meo"}).json()} == {24876}
    assert client.get("/api/sky/objects", params={"layer": "xx"}).status_code == 422


def test_chunk_endpoint_and_cache(client):
    body = {"layer": "meo", "site": SITE, "t0_unix": DAY0 + 7, "duration_s": 1800}
    r = client.post("/api/sky/chunk", json=body)
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    d = decode_chunk(r.content)
    assert d["header"]["t0_unix"] == DAY0 and d["header"]["step_s"] == 60
    r2 = client.post("/api/sky/chunk", json={**body, "t0_unix": DAY0 + 50})  # same grid cell
    assert r2.content == r.content
    head = client.post("/api/sky/chunk", json={**body, "duration_s": 120})
    assert decode_chunk(head.content)["header"]["n_steps"] == 3
    bad = client.post("/api/sky/chunk", json={**body, "layer": "xyz"})
    assert bad.status_code == 422
    assert client.post("/api/sky/chunk", json={**body, "duration_s": 99999}).status_code == 422


def test_search_rules(client):
    r = client.get("/api/sats/search").json()
    ids = [x["norad"] for x in r]
    assert ids == sorted(ids, reverse=True)  # latest (highest NORAD) first
    assert 11111 not in ids  # decayed excluded
    assert {22222, 33333} <= set(ids)  # non-payloads included
    assert set(r[0]) == {
        "norad", "name", "cospar", "country", "object_type", "orbit_class", "tle_epoch",
        "tle_age_days",
    }
    assert [x["norad"] for x in client.get("/api/sats/search", params={"q": "25544"}).json()] == [
        25544
    ]
    assert [x["norad"] for x in client.get("/api/sats/search", params={"q": "zarya"}).json()] == [
        25544
    ]
    rb = client.get("/api/sats/search", params={"object_type": "ROCKET_BODY"}).json()
    assert [x["norad"] for x in rb] == [22222]
    sso = client.get("/api/sats/search", params={"orbit_class": "sso"}).json()
    assert [x["norad"] for x in sso] == [43013]
    assert len(client.get("/api/sats/search", params={"limit": 2}).json()) == 2
    assert client.get("/api/sats/search", params={"limit": 501}).status_code == 422


def test_sat_detail(client):
    r = client.get("/api/sats/25544")
    assert r.status_code == 200
    j = r.json()
    assert j["tle_line1"].startswith("1 25544") and j["tle_line2"].startswith("2 25544")
    assert client.get("/api/sats/11111").status_code == 404  # decayed
    assert client.get("/api/sats/999999").status_code == 404


def test_refresh(client):
    r = client.post("/api/sats/refresh")
    assert r.status_code == 200 and r.json()["counts"] == {"leo": 2, "meo": 1, "geo": 3}


def test_unconfigured_gives_503():
    with TestClient(create_app(unconfigured_factory)) as c:
        time.sleep(1.5)
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/sky/objects", params={"layer": "leo"}).status_code == 503
        r = c.post("/api/sky/chunk", json={"layer": "leo", "site": SITE, "t0_unix": DAY0})
        assert r.status_code == 503
        assert c.get("/api/sats/search").status_code == 503
        assert c.get("/api/sats/status").json()["error"] == "unconfigured"


def test_health_latency_while_chunk_computes(tmp_path):
    path = str(tmp_path / "big.db")
    build_sqlite(path, extra_leo=6000)
    with TestClient(create_app(factory_for(path))) as c:
        _wait_ready(c, 120)
        done = threading.Event()
        out: dict = {}

        def run() -> None:
            t = time.perf_counter()
            r = c.post("/api/sky/chunk", json={"layer": "leo", "site": SITE, "t0_unix": DAY0})
            out["s"], out["code"] = time.perf_counter() - t, r.status_code
            done.set()

        th = threading.Thread(target=run)
        th.start()
        lat = []
        while not done.is_set():
            t = time.perf_counter()
            assert c.get("/api/health").status_code == 200
            lat.append(time.perf_counter() - t)
            time.sleep(0.02)
        th.join()
        assert out["code"] == 200
        assert len(lat) >= 3, f"chunk finished too fast to overlap ({out['s']:.2f}s)"
        assert max(lat) < 0.1, f"health took {max(lat):.3f}s during a chunk"


# ---- /api/sky/state (T2.3) -----------------------------------------------------------------


def _truth(norad: int, t_unix: float):
    from datetime import datetime, timezone

    from skyfield.api import EarthSatellite, load, load_file, wgs84

    from analysis.state import ephemeris_path
    from analysis.tests.fixture_catalog import ROWS, tle_of

    ts = load.timescale()
    name = next(r[1] for r in ROWS if r[0] == norad)
    sat = EarthSatellite(*tle_of(norad), name, ts)
    t = ts.from_datetime(datetime.fromtimestamp(t_unix, tz=timezone.utc))
    alt, az, dist = (sat - wgs84.latlon(SITE["lat"], SITE["lon"], SITE["alt_m"])).at(t).altaz()
    lit = bool(sat.at(t).is_sunlit(load_file(str(ephemeris_path()))))
    return az.degrees, alt.degrees, dist.km, lit, float(t.tt - sat.epoch.tt)


@pytest.mark.parametrize("norad", [25544, 24876, 41000, 39000])
def test_state_matches_skyfield(client, norad):
    t = DAY0 + 3 * 3600 + 17.5
    r = client.post("/api/sky/state", json={"norad": norad, "site": SITE, "t_unix": t})
    assert r.status_code == 200
    j = r.json()
    az, alt, rng, lit, age = _truth(norad, t)
    assert j["norad"] == norad and j["orbit_class"] and j["country"] == "TST"
    assert abs(j["az"] - az) < 1e-6 and abs(j["alt"] - alt) < 1e-6
    assert abs(j["range_km"] - rng) < 1e-3
    assert j["illuminated"] is lit
    assert abs(j["tle_age_days"] - age) < 1e-6
    assert set(j) == {
        "norad", "name", "cospar", "country", "object_type", "orbit_class", "az", "alt",
        "range_km", "illuminated", "tle_epoch", "tle_age_days",
    }


def test_state_agrees_with_chunk_time_grid(client):
    # Independent time path: the chunk pipeline (sgp4 jday grid). Regression for the 27 s
    # leap-second offset the state endpoint once had (ts.utc(1970, ..., t_unix)). Uses the LEO
    # fixture (a GEO object barely moves in 27 s, so it could not catch a time offset).
    import math

    from analysis.chunks import decode_chunk, iter_tracks

    for k in range(48):  # find a 30-min window of the fixture day in which the ISS is up
        t0 = DAY0 + k * 1800
        buf = client.post(
            "/api/sky/chunk", json={"layer": "leo", "site": SITE, "t0_unix": t0, "duration_s": 1800}
        ).content
        dec = decode_chunk(buf)
        tracks = [(s, smp) for n, s, smp in iter_tracks(dec) if n == 25544 and len(smp) >= 5]
        if tracks:
            break
    assert tracks, "ISS fixture never up during the fixture day"
    step, t0s = dec["header"]["step_s"], dec["header"]["t0_unix"]
    start, smp = tracks[0]
    for kk in range(1, len(smp) - 1):
        t = t0s + (start + kk) * step
        j = client.post("/api/sky/state", json={"norad": 25544, "site": SITE, "t_unix": t}).json()
        daz = (float(smp[kk][0]) - j["az"] + 180) % 360 - 180
        sep = math.hypot(daz * math.cos(math.radians(j["alt"])), float(smp[kk][1]) - j["alt"])
        assert sep < 0.02, (t, sep)


def test_state_default_time_nonlive_and_404(client):
    r = client.post("/api/sky/state", json={"norad": 22222, "site": SITE})  # non-payload: served
    assert r.status_code == 200 and r.json()["object_type"] == "ROCKET_BODY"
    assert client.post("/api/sky/state", json={"norad": 11111, "site": SITE}).status_code == 404
    assert client.post("/api/sky/state", json={"norad": 999999, "site": SITE}).status_code == 404
    bad = client.post("/api/sky/state", json={"norad": 25544, "site": {**SITE, "lat": 120}})
    assert bad.status_code == 422


def test_state_is_fast_when_warm(client):
    body = {"norad": 25544, "site": SITE, "t_unix": DAY0 + 100}
    client.post("/api/sky/state", json=body)
    ts = []
    for k in range(10):
        t = time.perf_counter()
        client.post("/api/sky/state", json={**body, "t_unix": DAY0 + 100 + k})
        ts.append(time.perf_counter() - t)
    assert sorted(ts)[len(ts) // 2] < 0.02


# ---- stale-TLE cutoff (T2.3 item 7) ---------------------------------------------------------


def test_stale_tle_absent_from_live_set_but_searchable(client):
    for layer in ("leo", "meo", "geo"):
        objs = client.get("/api/sky/objects", params={"layer": layer}).json()
        assert 44444 not in {o[0] for o in objs}
    assert client.get("/api/sats/status").json()["counts"]["leo"] == 2
    # not filtered: search, detail and state still serve it
    ids = [x["norad"] for x in client.get("/api/sats/search").json()]
    assert 44444 in ids
    stale = next(x for x in client.get("/api/sats/search").json() if x["norad"] == 44444)
    assert 150 < stale["tle_age_days"] < 250
    detail = client.get("/api/sats/44444")
    assert detail.status_code == 200 and 150 < detail.json()["tle_age_days"] < 250
    r = client.post("/api/sky/state", json={"norad": 44444, "site": SITE})
    assert r.status_code == 200 and r.json()["tle_age_days"] is not None
    # and never in a chunk
    for t0 in range(0, 86400, 3600):
        body = {"layer": "leo", "site": SITE, "t0_unix": DAY0 + t0}
        d = decode_chunk(client.post("/api/sky/chunk", json=body).content)
        assert 44444 not in d["header"]["norad"]
