"""T1.2 station settings: store, validation, seeding, secret hygiene, REST guard, site check."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.settings import (
    Location,
    MemorySecretStore,
    SettingsStore,
    StationSettings,
    compute_site_check,
)

from .conftest import FakePwi4, status_text

TOKEN = "t" * 43
GOOD_ORIGIN = "http://127.0.0.1:3200"
AUTH = {"x-mount-token": TOKEN}
KEY = "SECRET-ASTROMETRY-KEY-123"


def make_client(tmp_path, fake=None, secrets=None, poll=True):
    fake = fake or FakePwi4()
    secrets = secrets if secrets is not None else MemorySecretStore()
    settings = Settings(
        port=8400, token=TOKEN, settings_path=tmp_path / "s.json", poll_enabled=poll
    )
    app = create_app(settings, transport=fake.transport, secrets=secrets)
    return TestClient(app, base_url="http://127.0.0.1:8400"), fake, secrets, settings


def body(**over):
    d = StationSettings().model_dump()
    d["location"]["timezone"] = "UTC"
    d["location"].update(over.pop("location", {}))
    d["hardware"].update(over.pop("hardware", {}))
    d.update(over)
    return d


# ------------------------------------------------------------------ store


def test_store_roundtrip_and_atomic_write(tmp_path):
    p = tmp_path / "sub" / "settings.json"
    st = SettingsStore(p, MemorySecretStore())
    assert st.first_run and not p.exists()
    new = StationSettings.model_validate(
        body(location={"lat_deg": 12.5, "timezone": "Asia/Bangkok"})
    )
    st.save(new)
    assert p.exists() and not p.with_name("settings.json.tmp").exists()  # temp replaced
    again = SettingsStore(p, MemorySecretStore())
    assert not again.first_run
    assert again.current.location.lat_deg == 12.5
    assert again.current.location.timezone == "Asia/Bangkok"
    assert json.loads(p.read_text())["version"] == 1


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    st = SettingsStore(p, MemorySecretStore())
    assert st.first_run and st.current == StationSettings()
    assert p.with_suffix(".bad").exists()


@pytest.mark.parametrize(
    "loc",
    [
        {"lat_deg": 90.01},
        {"lat_deg": -90.01},
        {"lon_deg": 180.01},
        {"lon_deg": -180.01},
        {"alt_m": 9000.1},
        {"alt_m": -500.1},
        {"timezone": "Mars/Olympus"},
        {"timezone": ""},
    ],
)
def test_location_validation_rejects(loc):
    with pytest.raises(ValueError):
        Location(**loc)


def test_validation_edges_accepted():
    edges = ({"lat_deg": 90}, {"lat_deg": -90}, {"lon_deg": 180}, {"alt_m": 9000}, {"alt_m": -500})
    for edge in edges:
        Location(**edge)
    Location(timezone="Asia/Bangkok")
    for lim in (0, 90):
        StationSettings.model_validate(body(hardware={"lim_el_deg": lim}))
    for lim in (-0.1, 90.1):
        with pytest.raises(ValueError):
            StationSettings.model_validate(body(hardware={"lim_el_deg": lim}))


# ------------------------------------------------------------------ seeding


def test_first_run_seeds_from_pwi4_when_up(tmp_path):
    c, fake, _, s = make_client(tmp_path)
    with c:
        r = c.get("/api/mount/settings", headers=AUTH).json()
    assert r["location"]["lat_deg"] == pytest.approx(14.3479)
    assert r["location"]["lon_deg"] == pytest.approx(100.5656)
    assert r["location"]["alt_m"] == pytest.approx(12)
    assert r["location"]["timezone"] is None and r["hardware"]["lim_el_deg"] == 15
    assert s.settings_path.exists()  # seeded value persisted


def test_first_run_defaults_when_pwi4_down(tmp_path):
    fake = FakePwi4()
    fake.down = True
    c, _, _, _ = make_client(tmp_path, fake)
    with c:
        r = c.get("/api/mount/settings", headers=AUTH).json()
    assert r["location"]["lat_deg"] == pytest.approx(13.7862)
    assert r["location"]["lon_deg"] == pytest.approx(101.4639)
    assert r["location"]["alt_m"] == pytest.approx(40.8)


def test_existing_file_is_not_reseeded(tmp_path):
    c, _, _, s = make_client(tmp_path)
    with c:
        c.put("/api/mount/settings", headers=AUTH, json=body(location={"lat_deg": 1.0}))
    c2, _, _, _ = make_client(tmp_path)  # PWI4 site differs, file wins
    with c2:
        assert c2.get("/api/mount/settings", headers=AUTH).json()["location"]["lat_deg"] == 1.0


# ------------------------------------------------------------------ REST / secret


def test_token_required_401(tmp_path):
    c, *_ = make_client(tmp_path)
    with c:
        for method, path in (
            ("get", "/api/mount/settings"),
            ("put", "/api/mount/settings"),
            ("get", "/api/mount/site-check"),
            ("post", "/api/mount/site-check"),
        ):
            r = getattr(c, method)(path)
            assert r.status_code == 401, path
            r = getattr(c, method)(path, headers={"x-mount-token": "wrong"})
            assert r.status_code == 401, path


def test_origin_and_host_enforced(tmp_path):
    c, *_ = make_client(tmp_path)
    with c:
        r = c.get("/api/mount/settings", headers={**AUTH, "origin": "http://evil.example"})
        assert r.status_code == 403
        r = c.get("/api/mount/settings", headers={**AUTH, "sec-fetch-site": "cross-site"})
        assert r.status_code == 403
        r = c.get("/api/mount/settings", headers={**AUTH, "origin": GOOD_ORIGIN})
        assert r.status_code == 200
        r = c.get("/api/mount/settings", headers={**AUTH, "host": "evil.example:8400"})
        assert r.status_code == 400


def test_timezone_null_only_as_first_run_value(tmp_path):
    c, *_ = make_client(tmp_path)
    with c:
        assert c.get("/api/mount/settings", headers=AUTH).json()["location"]["timezone"] is None
        nul = body(location={"timezone": None})
        assert c.put("/api/mount/settings", headers=AUTH, json=nul).status_code == 422
        r = c.put("/api/mount/settings", headers=AUTH, json=body(location={"timezone": "UTC"}))
        assert r.status_code == 200 and r.json()["location"]["timezone"] == "UTC"  # UTC is kept
        assert c.get("/api/mount/settings", headers=AUTH).json()["location"]["timezone"] == "UTC"


def test_put_validation_422(tmp_path):
    c, *_ = make_client(tmp_path)
    with c:
        for bad in (
            body(location={"lat_deg": 91}),
            body(location={"timezone": "Nope/Zone"}),
            body(hardware={"lim_el_deg": 91}),
        ):
            assert c.put("/api/mount/settings", headers=AUTH, json=bad).status_code == 422


def test_api_key_never_in_file_responses_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c, _, secrets, s = make_client(tmp_path)
    with c:
        r = c.put("/api/mount/settings", headers=AUTH, json={**body(), "api_key": KEY})
        assert r.status_code == 200 and r.json()["api_key_set"] is True
        assert KEY not in r.text and "api_key\"" not in r.text.replace("api_key_set", "")
        g = c.get("/api/mount/settings", headers=AUTH)
        assert g.json()["api_key_set"] is True and KEY not in g.text
        assert KEY not in c.get("/api/mount/site-check", headers=AUTH).text
        assert KEY not in c.get("/api/mount/status").text
    assert secrets.get() == KEY  # it did reach the secret store
    assert KEY not in s.settings_path.read_text()
    assert "api_key" not in json.loads(s.settings_path.read_text())
    assert KEY not in caplog.text
    # no stray files carrying it either
    assert all(KEY not in f.read_text(errors="ignore") for f in tmp_path.rglob("*") if f.is_file())


def test_api_key_set_keep_clear(tmp_path):
    c, _, secrets, _ = make_client(tmp_path)
    with c:
        c.put("/api/mount/settings", headers=AUTH, json={**body(), "api_key": KEY})
        # absent -> keep, null -> keep
        c.put("/api/mount/settings", headers=AUTH, json=body())
        assert secrets.get() == KEY
        c.put("/api/mount/settings", headers=AUTH, json={**body(), "api_key": None})
        assert secrets.get() == KEY
        r = c.put("/api/mount/settings", headers=AUTH, json={**body(), "api_key": ""})
        assert secrets.get() is None and r.json()["api_key_set"] is False


def test_put_applies_live_to_snapshot_and_sunmoon(tmp_path):
    fake = FakePwi4()
    fake.down = True  # PWI4 down: Sun/Moon still come from the Settings site
    c, *_ = make_client(tmp_path, fake)
    with c:
        c.put(
            "/api/mount/settings",
            headers=AUTH,
            json=body(
                location={
                    "lat_deg": 10.0,
                    "lon_deg": 99.0,
                    "alt_m": 5.0,
                    "timezone": "Asia/Bangkok",
                },
                hardware={"lim_el_deg": 20},
            ),
        )
        import time

        snap = None
        for _ in range(60):
            r = c.get("/api/mount/status")
            if r.status_code == 200:
                snap = r.json()
                if snap["site"]["lat"] == 10.0 and snap["sun"]["alt"] is not None:
                    break
            time.sleep(0.05)
    assert snap["site"] == {
        "lat": 10.0,
        "lon": 99.0,
        "height_m": 5.0,
        "source": "settings",
        "timezone": "Asia/Bangkok",
    }
    assert snap["state"] == "DISCONNECTED"
    if snap["sun"]["alt"] is None:
        pytest.skip("ephemeris file not available locally")
    assert snap["moon"]["alt"] is not None
    assert snap["limits"] == {"lim_el_deg": 20.0, "sun_keepout_deg": 15.0}
    assert snap["time"]["lmst_hours"] is not None
    assert snap["site_check"]["pwi4_reachable"] is False


def test_pwi4_receives_only_status(tmp_path):
    c, fake, *_ = make_client(tmp_path)
    with c:
        c.put("/api/mount/settings", headers=AUTH, json=body())
        c.get("/api/mount/site-check", headers=AUTH)
    assert fake.paths and set(fake.paths) == {"GET /status"}


# ------------------------------------------------------------------ site check


def loc(**kw):
    return Location(**{"lat_deg": 14.3479, "lon_deg": 100.5656, "alt_m": 12.0, **kw})


PWI4_SITE = (14.3479, 100.5656, 12.0)


def test_site_check_equal_ok():
    sc = compute_site_check(loc(), PWI4_SITE)
    assert sc["ok"] is True and sc["pwi4_reachable"] is True
    assert sc["tolerance"] == {"lat_lon_deg": 0.01, "alt_m": 100.0}


@pytest.mark.parametrize(
    ("kw", "ok"),
    [
        ({"lat_deg": 14.3479 + 0.0099}, True),
        ({"lat_deg": 14.3479 + 0.0101}, False),
        ({"lat_deg": 14.3479 - 0.0101}, False),
        ({"lon_deg": 100.5656 + 0.0099}, True),
        ({"lon_deg": 100.5656 + 0.0101}, False),
        ({"alt_m": 12 + 99}, True),
        ({"alt_m": 12 + 101}, False),
        ({"alt_m": 12 - 101}, False),
    ],
)
def test_site_check_tolerance_edges(kw, ok):
    assert compute_site_check(loc(**kw), PWI4_SITE)["ok"] is ok


def test_site_check_pwi4_longitude_wrap():
    sc = compute_site_check(loc(lon_deg=-179.999), (14.3479, 180.001, 12.0))
    assert sc["ok"] is True and abs(sc["lon_diff_deg"]) < 0.01


def test_site_check_unreachable_nulls():
    sc = compute_site_check(loc(), None)
    assert sc["pwi4_reachable"] is False and sc["ok"] is None
    assert sc["lat_diff_deg"] is None and sc["lon_diff_deg"] is None and sc["alt_diff_m"] is None


def test_site_check_route_mismatch_and_match(tmp_path):
    fake = FakePwi4()
    c, *_ = make_client(tmp_path, fake)
    with c:
        r = c.put("/api/mount/settings", headers=AUTH, json=body(location={"lat_deg": 13.7862}))
        sc = r.json()["site_check"]
        assert sc["ok"] is False and sc["lat_diff_deg"] == pytest.approx(13.7862 - 14.3479)
        same = body(location={"lat_deg": 14.3479, "lon_deg": 100.5656, "alt_m": 12.0})
        r = c.put("/api/mount/settings", headers=AUTH, json=same)
        assert r.json()["site_check"]["ok"] is True
        fake.text = status_text({"site.latitude_degs": 20.0})
        import time

        time.sleep(0.4)
        assert c.get("/api/mount/site-check", headers=AUTH).json()["ok"] is False
        fake.down = True
        time.sleep(1.4)
        sc = c.get("/api/mount/site-check", headers=AUTH).json()
        assert sc["pwi4_reachable"] is False and sc["ok"] is None


def test_site_check_post_proposed_is_read_only(tmp_path):
    import time

    fake = FakePwi4()
    c, _, _, settings = make_client(tmp_path, fake)
    with c:
        time.sleep(0.4)
        saved = c.put("/api/mount/settings", headers=AUTH, json=body()).json()["location"]
        before = (tmp_path / "s.json").read_text(encoding="utf-8")
        paths_before = len(fake.paths)
        url = "/api/mount/site-check"
        prop = {"lat_deg": 13.0, "lon_deg": 101.0, "alt_m": 5.0}
        bad = c.post(url, headers=AUTH, json=prop)
        assert bad.status_code == 200 and bad.json()["ok"] is False
        assert bad.json()["lat_diff_deg"] == pytest.approx(13.0 - 14.3479)
        good = c.post(
            "/api/mount/site-check",
            headers=AUTH,
            json={"lat_deg": 14.3479, "lon_deg": 100.5656, "alt_m": 12.0},
        )
        assert good.json()["ok"] is True
        # nothing saved; the saved location is unchanged
        assert (tmp_path / "s.json").read_text(encoding="utf-8") == before
        assert c.get("/api/mount/settings", headers=AUTH).json()["location"] == saved
        # read-only towards PWI4: only polling GET /status, never anything else
        assert all(p == "GET /status" for p in fake.paths[paths_before:])
        # bad input -> 422
        assert c.post(url, headers=AUTH, json={"lat_deg": 95}).status_code == 422
        assert c.post(url, headers=AUTH, json={"lat_deg": "x"}).status_code == 422
        # PWI4 unreachable -> ok null
        fake.down = True
        time.sleep(1.4)
        r = c.post("/api/mount/site-check", headers=AUTH, json={"lat_deg": 1.0})
        assert r.json()["pwi4_reachable"] is False and r.json()["ok"] is None
