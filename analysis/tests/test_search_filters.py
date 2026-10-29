from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from analysis.app import create_app
from analysis.tests.fixture_catalog import build_sqlite, factory_for
from analysis.tests.test_service import _wait_ready


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("flt") / "cat.db")
    build_sqlite(path)
    with TestClient(create_app(factory_for(path))) as c:
        _wait_ready(c)
        yield c


def ids(c, **params) -> list[int]:
    r = c.get("/api/sats/search", params=params)
    assert r.status_code == 200
    return [x["norad"] for x in r.json()]


def test_country_exact_case_insensitive(client):
    assert ids(client, country="US") == [44444, 41001, 22222]  # NORAD desc, stale + RB kept
    assert ids(client, country="us") == ids(client, country="US")
    assert ids(client, country="PRC") == [43013]  # decayed 11111 (PRC) excluded
    assert ids(client, country="U") == []  # exact, not prefix
    assert ids(client, country="XX") == []


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        ("leo", [44444, 43013, 25544, 22222]),  # LEO + SSO
        ("meo", [24876]),
        ("geo", [41001, 41000, 39000]),  # GEO + HEO
        ("GEO", [41001, 41000, 39000]),
    ],
)
def test_orbit_group_mapping(client, group, expected):
    assert ids(client, orbit_group=group) == expected


def test_orbit_group_bad_is_422(client):
    for bad in ("sso", "heo", "xyz", ""):
        assert client.get("/api/sats/search", params={"orbit_group": bad}).status_code == 422


def test_combined_filters(client):
    assert ids(client, country="US", orbit_group="leo") == [44444, 22222]
    assert ids(client, country="US", orbit_group="leo", object_type="rocket_body") == [22222]
    assert ids(client, country="TST", orbit_group="geo") == [41000, 39000]
    assert ids(client, orbit_group="leo", orbit_class="sso") == [43013]
    assert ids(client, orbit_group="meo", orbit_class="leo") == []
    assert ids(client, country="US", q="stale") == [44444]
    assert ids(client, orbit_group="leo", limit=2) == [44444, 43013]


def test_unfiltered_unchanged(client):
    got = ids(client)
    assert got == sorted(got, reverse=True) and 11111 not in got and len(got) == 9


def test_countries_ordering_and_shape(client):
    r = client.get("/api/sats/countries")
    assert r.status_code == 200
    assert r.json() == [
        {"code": "TST", "count": 4},
        {"code": "US", "count": 3},
        {"code": "PRC", "count": 1},
    ]  # NULL country and decayed object not counted


def test_countries_route_not_shadowed(client):
    # would be a 422 (int parse of "countries") if captured by /api/sats/{norad}
    assert client.get("/api/sats/countries").status_code == 200
    assert client.get("/api/sats/25544").json()["norad"] == 25544
    assert client.get("/api/sats/99999999").status_code == 404


def test_countries_cached_until_refresh(client):
    svc = client.app.state.service
    first = client.get("/api/sats/countries").json()
    assert svc._countries == first
    assert client.get("/api/sats/countries").json() == first
    assert client.post("/api/sats/refresh").json()["ok"]
    assert svc._countries is None
    assert client.get("/api/sats/countries").json() == first
