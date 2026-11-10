"""T5.4b ADS-B integration: optional import, both sources, the adapter vs predict.path, the green
rule, secret hygiene, the 1 Hz message and isolation from the 10 Hz status stream.

Only fakes: a fake PlatformClient and a fake adsb_interpreter feed (fake_adsb.py) on a throwaway
port. No real platform, no 8500."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
import time

import pytest
from fastapi.testclient import TestClient

from gateway import adsb as adsb_mod
from gateway.adsb import AdsbRunner, adapt_feed_aircraft, is_green, load_core
from gateway.app import create_app
from gateway.config import Settings
from gateway.hub import Hub
from gateway.settings import (
    ADSB_KEYRING_USER,
    KeyringSecretStore,
    MemorySecretStore,
    SettingsStore,
    StationSettings,
)

from . import fake_adsb
from .conftest import FakePwi4

adsb_core = pytest.importorskip("adsb_core")
pytestmark = pytest.mark.skipif(
    not str(adsb_core.__version__).startswith("0.3."), reason="needs adsb_core 0.3.x"
)

TOKEN = "t" * 43
AUTH = {"x-mount-token": TOKEN}
GOOD_ORIGIN = "http://127.0.0.1:3200"
WS_GOOD = {"origin": GOOD_ORIGIN, "host": "127.0.0.1:8400"}
READ_TOKEN = "PLATFORM-READ-TOKEN-ABC123XYZ"
URL = "wss://10.22.123.68/api/v1/ws"


def make_store(tmp_path, *, enabled=True, token=READ_TOKEN, range_km=100) -> SettingsStore:
    store = SettingsStore(tmp_path / "s.json", MemorySecretStore(), MemorySecretStore(),
                          MemorySecretStore(token))
    cur = store.current.model_copy(deep=True)
    cur.adsb.enabled = enabled
    cur.adsb.range_limit_km = range_km
    cur.location.timezone = "UTC"
    store.save(cur)
    return store


async def until(pred, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


# ---------------------------------------------------------------- the optional import


def test_load_core_ok():
    core, why = load_core()
    assert core is not None and why == "" and core.version.startswith("0.3.")


def test_load_core_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "adsb_core", None)  # import raises
    core, why = load_core()
    assert core is None and why == "ADS-B module not installed"


def test_load_core_wrong_version(monkeypatch):
    monkeypatch.setattr(adsb_core, "__version__", "0.4.1")
    core, why = load_core()
    assert core is None and "0.4.1" in why and "0.3.x" in why


def test_load_core_broken_submodule(monkeypatch):
    monkeypatch.setitem(sys.modules, "adsb_core.predict", None)
    core, why = load_core()
    assert core is None and "failed to load" in why


def test_gateway_starts_and_works_without_the_module(tmp_path):
    fake = FakePwi4()
    s = Settings(port=8400, token=TOKEN, settings_path=tmp_path / "s.json", poll_enabled=True)
    app = create_app(
        s,
        transport=fake.transport,
        secrets=MemorySecretStore(),
        adsb_loader=lambda: (None, "ADS-B module not installed"),
    )
    with TestClient(app, base_url="http://127.0.0.1:8400") as c:
        assert c.get("/api/mount/health").json()["ok"] is True
        d = None
        for _ in range(100):
            r = c.get("/api/mount/status")
            if r.status_code == 200:
                d = r.json()
                break
            time.sleep(0.05)
        assert d is not None and d["state"] == "STOPPED"
        assert d["adsb"]["state"] == "module_missing" and d["adsb"]["green"] is False
        assert d["adsb"]["detail"] == "ADS-B module not installed"
        st = c.get("/api/mount/settings", headers=AUTH).json()
        assert st["adsb"]["status"]["state"] == "module_missing"
        # saving settings with the module missing is fine too
        put = StationSettings().model_dump()
        put["location"]["timezone"] = "UTC"
        put["adsb"]["enabled"] = True
        assert c.put("/api/mount/settings", headers=AUTH, json=put).status_code == 200
        assert c.get("/api/mount/status").json()["adsb"]["state"] == "module_missing"


# ---------------------------------------------------------------- the green rule


@pytest.mark.parametrize(
    ("state", "simulated", "green"),
    [
        ("connected", False, True),
        ("stale", False, True),
        ("connecting", False, False),
        ("unreachable", False, False),
        ("auth_failed", False, False),
        ("tls_error", False, False),
        ("error", False, False),
        ("not_configured", False, False),
        ("disabled", False, False),
        ("module_missing", False, False),
        ("stopped", False, False),
        ("simulated", True, True),
        ("connecting", True, False),
        ("unreachable", True, False),
        ("auth_failed", True, False),
        ("error", True, False),
        ("connected", True, False),
        ("module_missing", True, False),
    ],
)
def test_green_rule(state, simulated, green):
    assert is_green(state, simulated) is green


def test_runner_not_green_states_have_readable_details(tmp_path):
    async def go():
        out = {}
        hub = Hub()
        # disabled
        r = AdsbRunner(make_store(tmp_path, enabled=False), hub, simulated=False)
        await r.apply()
        out["disabled"] = r.block()
        # enabled but no token
        r = AdsbRunner(make_store(tmp_path, token=None), hub, simulated=False)
        await r.apply()
        out["not_configured"] = r.block()
        # module missing
        r = AdsbRunner(make_store(tmp_path), hub, simulated=False,
                       loader=lambda: (None, "ADS-B module not installed"))
        await r.apply()
        out["module_missing"] = r.block()
        await r.stop()
        return out

    out = asyncio.run(go())
    for state, b in out.items():
        assert b["state"] == state and b["green"] is False and len(b["detail"]) > 8, b
    assert "Settings" in out["disabled"]["detail"] and "token" in out["not_configured"]["detail"]


# ---------------------------------------------------------------- the adapter vs predict.path


def test_adapter_feeds_predict_path():
    core, _ = load_core()
    t = 1_800_000_000.0
    row = fake_adsb.offset_row("AAA111", 0.0, 10.0, 10000.0, callsign="TST1", gs=450.0,
                               track=90.0, vrate=-640.0, turn=0.0, pos_age=1.5)
    ac = adapt_feed_aircraft(core, row, t)
    assert ac.icao == "AAA111" and ac.callsign == "TST1"
    assert ac.fix_t == pytest.approx(t - 1.5)
    assert ac.velocity_t == ac.fix_t
    assert ac.east_kt == pytest.approx(450.0) and ac.north_kt == pytest.approx(0.0, abs=1e-6)
    assert ac.vertical_rate_fpm == -640.0 and ac.height_m == 10000.0
    pts = core.predict.path(ac, t - 0.5, t + 10.0, 0.5)
    assert len(pts) == 22 and all(p.method == "velocity" for _, p in pts)
    # due east at 450 kt (231.5 m/s): 11.5 s after the fix -> ~2.66 km east of it
    t_last, last = pts[-1]
    assert t_last == pytest.approx(t + 10.0)
    east_m = (last.lon - ac.lon) * 111_320.0 * 0.9718  # cos(13.8 deg)
    assert east_m == pytest.approx(231.5 * 11.5, rel=0.02)
    assert last.lat == pytest.approx(ac.lat, abs=1e-4)
    assert last.height_m == pytest.approx(10000.0 - 640 * 0.3048 / 60 * 11.5, abs=1.0)


def test_adapter_without_velocity_uses_kalman_after_three_fixes():
    core, _ = load_core()
    t = 1_800_000_000.0
    prev = None
    for k in range(4):
        row = fake_adsb.offset_row("BBB222", 0.2 * k, 12.0, 8000.0, gs=None, track=None,
                                   pos_age=0.5)
        prev = adapt_feed_aircraft(core, row, t + k, prev)
        pts = core.predict.path(prev, t + k, t + k + 2, 1.0)
        assert bool(pts) is (k >= 2)  # CvKalman.MIN_UPDATES = 3 distinct fixes
    assert all(p.method == "kalman" for _, p in pts)
    # a repeated snapshot of the same fix keeps the fix time and does not feed the filter again
    again = adapt_feed_aircraft(core, row, t + 3.4, prev)
    assert again.fix_t == prev.fix_t and again.kalman.updates == 4


def test_adapter_without_position_has_no_path():
    core, _ = load_core()
    row = fake_adsb.feed_row("CCC333", None, None, None, pos_age=None)
    ac = adapt_feed_aircraft(core, row, 1_800_000_000.0)
    assert ac.fix_t is None and core.predict.path(ac, 0, 5, 1.0) == []
    assert adapt_feed_aircraft(core, {"icao": ""}, 0.0) is None


# ---------------------------------------------------------------- live source (fake client)


def test_live_source_end_to_end(tmp_path):
    async def go():
        hub = Hub()
        events = fake_adsb.overflight_events(seconds=14)

        async def script(client):
            client.status("connecting")
            client.status("connected")
            for ev in events:
                client.on_event(ev)
                client.status("connected", "", time.time())
            await asyncio.sleep(3600)

        r = AdsbRunner(make_store(tmp_path), hub, simulated=False,
                       client_factory=fake_adsb.factory(script))
        await r.start()
        ok = await until(lambda: bool(json.loads(r.tick())["data"]["aircraft"]))
        msg = json.loads(r.tick())
        ac = r.aircraft_for("ABC123")
        client = fake_adsb.FakeClient.instances[0]
        await r.stop()
        return ok, msg, ac, client

    ok, msg, ac, client = asyncio.run(go())
    assert ok
    d = msg["data"]
    assert d["adsb"]["state"] == "connected" and d["adsb"]["green"] is True
    assert d["adsb"]["simulated"] is False
    assert d["adsb"]["counts"]["messages"] > 20 and d["adsb"]["counts"]["aircraft"] == 1
    row = d["aircraft"][0]
    assert set(row) == {
        "icao", "callsign", "lat", "lon", "az", "el", "range_m", "track_deg", "alt_ft",
        "gs_kt", "vrate_fpm", "pos_age_s", "stale", "height_source", "predictor",
    }
    assert row["icao"] == "ABC123" and row["callsign"] == "TST100"
    assert 0 < row["el"] < 90 and 8_000 < row["range_m"] < 40_000
    assert row["alt_ft"] == 35000 and row["predictor"] == "velocity"
    assert row["height_source"] == "baro+diff" and row["track_deg"] == 90.0
    assert row["stale"] is False
    assert isinstance(row["lat"], float) and isinstance(row["lon"], float)
    assert round(row["lat"], 5) == row["lat"] and round(row["lon"], 5) == row["lon"]
    # predict.path works on the live aircraft too (one cueing path for both sources)
    core, _ = load_core()
    assert len(core.predict.path(ac, time.time() - 0.5, time.time() + 10, 0.5)) == 22
    # the client got the stored token and the URL; the kinds default is the client's business
    assert client.token == READ_TOKEN and client.url == URL


def test_live_states_map_and_never_leak_the_token(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        hub = Hub()
        seen = {}

        async def script(client):
            for state, detail in (
                ("unreachable", f"unreachable (OSError) {READ_TOKEN}"),
                ("auth_failed", "stream rejected (1008)"),
                ("tls_error", "TLS verification failed (verify code 20)"),
                ("error", "platform URL must be wss://host/..."),
                ("stale", "no event for 60 s"),
                ("connected", ""),
            ):
                client.status(state, detail)
                await asyncio.sleep(0.05)
                seen[state] = (r.block(), json.loads(r.tick()))
            await asyncio.sleep(3600)

        r = AdsbRunner(make_store(tmp_path), hub, simulated=False,
                       client_factory=fake_adsb.factory(script))
        await r.start()
        await until(lambda: "connected" in seen)
        await r.stop()
        return seen

    seen = asyncio.run(go())
    for state, (b, m) in seen.items():
        assert b["state"] == state
        assert b["green"] is (state in ("stale", "connected"))
        assert READ_TOKEN not in json.dumps(m) and READ_TOKEN not in b["detail"]
    assert "unreachable" in seen["unreachable"][0]["detail"].lower()
    assert "token" in seen["auth_failed"][0]["detail"]
    assert "TLS" in seen["tls_error"][0]["detail"]
    assert READ_TOKEN not in caplog.text


def test_not_green_hides_the_aircraft_list(tmp_path):
    async def go():
        hub = Hub()

        async def script(client):
            client.status("connected")
            for ev in fake_adsb.overflight_events(seconds=14):
                client.on_event(ev)
            await asyncio.sleep(0.3)
            client.status("auth_failed", "stream rejected (1008)")
            await asyncio.sleep(3600)

        r = AdsbRunner(make_store(tmp_path), hub, simulated=False,
                       client_factory=fake_adsb.factory(script))
        await r.start()
        await until(lambda: r.state == "connected" and r._source.table.get("ABC123") is not None)
        shown = json.loads(r.tick())["data"]["aircraft"]
        await until(lambda: r.state == "auth_failed")
        hidden = json.loads(r.tick())["data"]
        await r.stop()
        return shown, hidden

    shown, hidden = asyncio.run(go())
    assert len(shown) == 1
    assert hidden["aircraft"] == [] and hidden["adsb"]["green"] is False


def test_range_limit_and_horizon_filters(tmp_path):
    """Within the Settings range limit and above 0 deg only."""

    async def go(range_km):
        hub = Hub()

        async def script(client):
            client.status("connected")
            for ev in fake_adsb.overflight_events("AA0001", seconds=14, north_km=10):
                client.on_event(ev)
            for ev in fake_adsb.overflight_events("AA0002", seconds=14, north_km=45,
                                                  alt_ft=35000):
                client.on_event(ev)
            await asyncio.sleep(3600)

        r = AdsbRunner(make_store(tmp_path, range_km=range_km), hub, simulated=False,
                       client_factory=fake_adsb.factory(script))
        await r.start()
        await until(lambda: len(r._source.table) == 2 and all(
            a.fix_t is not None for a in r._source.table))
        rows = json.loads(r.tick())["data"]["aircraft"]
        await r.stop()
        return [x["icao"] for x in rows]

    assert asyncio.run(go(100)) == ["AA0001", "AA0002"]
    # AA0002 is ~45+ km: out at 20 km (and the table itself rejects fixes beyond the limit)
    assert asyncio.run(go(20)) == ["AA0001"]


# ---------------------------------------------------------------- simulated source (fake feed)


def sim_rows():
    return [
        fake_adsb.offset_row("SIM001", 0.0, 10.0, 10000.0, callsign="SIM1", pos_age=0.5),
        fake_adsb.offset_row("SIM002", 3.0, 15.0, 8000.0, callsign=None, gs=None, track=None,
                             pos_age=2.0),
        fake_adsb.offset_row("SIM003", 150.0, 0.0, 10000.0, pos_age=0.5),  # beyond 100 km
        fake_adsb.offset_row("SIM004", 30.0, 0.0, 20.0, pos_age=0.5),  # below the horizon
        fake_adsb.offset_row("SIM005", 0.0, 12.0, 9000.0, pos_age=15.0),  # stale
        fake_adsb.feed_row("SIM006", None, None, None, pos_age=None),  # no position
    ]


def test_simulated_source_end_to_end(tmp_path):
    async def go():
        async with fake_adsb.FakeFeed(sim_rows()) as feed:
            hub = Hub()
            r = AdsbRunner(make_store(tmp_path, enabled=False, token=None), hub, simulated=True,
                           feed_url=feed.url, feed_backoff_s=(0.1, 0.3))
            await r.start()
            ok = await until(lambda: r.state == "simulated")
            await until(lambda: len(r._source.aircraft()) == 6)
            await asyncio.sleep(0.25)
            msg = json.loads(r.tick())
            ac = r.aircraft_for("SIM001")
            sess = list(feed.session_calls)
            auth_ok = feed.auth_ok
            await r.stop()
            return ok, msg, ac, sess, auth_ok

    ok, msg, ac, sess, auth_ok = asyncio.run(go())
    assert ok and auth_ok == 1
    assert sess[0]["sec-fetch-site"] == "none"
    d = msg["data"]
    assert msg["type"] == "aircraft"
    assert d["adsb"]["state"] == "simulated" and d["adsb"]["green"] is True
    assert d["adsb"]["simulated"] is True and "8500" in d["adsb"]["detail"]
    assert d["adsb"]["counts"]["aircraft"] == 6
    icaos = [a["icao"] for a in d["aircraft"]]
    assert icaos == ["SIM001", "SIM002", "SIM005"]  # range, horizon, no-position rows dropped
    by = {a["icao"]: a for a in d["aircraft"]}
    assert by["SIM001"]["predictor"] == "velocity" and by["SIM001"]["callsign"] == "SIM1"
    assert by["SIM002"]["predictor"] is None  # no velocity and fewer than 3 fixes yet
    assert by["SIM005"]["stale"] is True and by["SIM001"]["stale"] is False
    assert by["SIM001"]["el"] == pytest.approx(
        math.degrees(math.atan2(10000.0 - 10.8, 10000.0)), abs=2.5,
    )
    core, _ = load_core()
    assert len(core.predict.path(ac, time.time() - 0.5, time.time() + 10, 0.5)) == 22


def test_simulated_not_green_cases(tmp_path):
    async def go():
        out = {}
        hub = Hub()
        # nothing listens on the feed port
        r = AdsbRunner(make_store(tmp_path), hub, simulated=True, feed_url="http://127.0.0.1:18759",
                       feed_backoff_s=(0.1, 0.2))
        await r.start()
        await until(lambda: r.state == "unreachable")
        out["down"] = r.block()
        await r.stop()
        # the feed is up but is not in simulated mode
        async with fake_adsb.FakeFeed(sim_rows(), state="stopped") as feed:
            r = AdsbRunner(make_store(tmp_path), hub, simulated=True, feed_url=feed.url,
                           feed_backoff_s=(0.1, 0.2))
            await r.start()
            await until(lambda: r.state == "error")
            out["notsim"] = (r.block(), json.loads(r.tick())["data"]["aircraft"])
            await r.stop()
        # the session is refused
        async with fake_adsb.FakeFeed(sim_rows()) as feed:
            feed.refuse_session = True
            r = AdsbRunner(make_store(tmp_path), hub, simulated=True, feed_url=feed.url,
                           feed_backoff_s=(0.1, 0.2))
            await r.start()
            await until(lambda: r.state == "auth_failed")
            out["refused"] = r.block()
            await r.stop()
        return out

    out = asyncio.run(go())
    assert out["down"]["state"] == "unreachable" and out["down"]["green"] is False
    b, rows = out["notsim"]
    assert b["state"] == "error" and b["green"] is False and "simulated" in b["detail"]
    assert rows == []
    assert out["refused"]["state"] == "auth_failed" and out["refused"]["green"] is False


def test_simulated_reconnects_after_the_feed_restarts(tmp_path):
    async def go():
        hub = Hub()
        feed = fake_adsb.FakeFeed(sim_rows())
        await feed.__aenter__()
        port = feed.port
        r = AdsbRunner(make_store(tmp_path), hub, simulated=True, feed_url=feed.url,
                       feed_backoff_s=(0.1, 0.2))
        await r.start()
        assert await until(lambda: r.state == "simulated")
        await feed.__aexit__(None, None, None)
        gone = await until(lambda: r.state == "unreachable", 8.0)
        feed2 = fake_adsb.FakeFeed(sim_rows())
        feed2.port = port
        feed2._server = await fake_adsb.serve(
            feed2._handler, "127.0.0.1", port, process_request=feed2._process_request
        )
        back = await until(lambda: r.state == "simulated", 8.0)
        await r.stop()
        await feed2.__aexit__(None, None, None)
        return gone, back

    gone, back = asyncio.run(go())
    assert gone and back


# ---------------------------------------------------------------- settings, secrets, restart


def make_app(tmp_path, **kw):
    fake = FakePwi4()
    secrets = MemorySecretStore()
    adsb_secrets = MemorySecretStore()
    s = Settings(port=8400, token=TOKEN, settings_path=tmp_path / "s.json", poll_enabled=True,
                 audit_dir=tmp_path / "audit")
    app = create_app(s, transport=fake.transport, secrets=secrets, adsb_secrets=adsb_secrets, **kw)
    return TestClient(app, base_url="http://127.0.0.1:8400"), adsb_secrets, s


def body(**adsb):
    d = StationSettings().model_dump()
    d["location"]["timezone"] = "UTC"
    d["adsb"].update(adsb)
    return d


def test_keyring_entry_name():
    assert ADSB_KEYRING_USER == "adsb_read_token"
    assert KeyringSecretStore(ADSB_KEYRING_USER).user == "adsb_read_token"


def test_token_is_write_only_everywhere(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c, secrets, s = make_app(tmp_path, adsb_client_factory=fake_adsb.factory())
    with c:
        r = c.put("/api/mount/settings", headers=AUTH,
                  json={**body(enabled=True), "adsb_token": READ_TOKEN})
        assert r.status_code == 200
        a = r.json()["adsb"]
        assert a["token_set"] is True and a["enabled"] is True and a["url"] == URL
        assert a["range_limit_km"] == 100 and a["simulated"] is False
        assert READ_TOKEN not in r.text
        g = c.get("/api/mount/settings", headers=AUTH)
        assert READ_TOKEN not in g.text and g.json()["adsb"]["token_set"] is True
        assert "adsb_token" not in g.json()
        for path in ("/api/mount/status", "/api/mount/site-check"):
            assert READ_TOKEN not in c.get(path, headers=AUTH).text
        # absent / null keep, "" clears
        c.put("/api/mount/settings", headers=AUTH, json=body(enabled=True))
        assert secrets.get() == READ_TOKEN
        c.put("/api/mount/settings", headers=AUTH, json={**body(), "adsb_token": None})
        assert secrets.get() == READ_TOKEN
        r = c.put("/api/mount/settings", headers=AUTH, json={**body(), "adsb_token": ""})
        assert secrets.get() is None and r.json()["adsb"]["token_set"] is False
        c.put("/api/mount/settings", headers=AUTH,
              json={**body(enabled=True), "adsb_token": READ_TOKEN})
        # the aircraft message and the 10 Hz status never carry it either
        with c.websocket_connect("/api/mount/ws", headers=WS_GOOD) as ws:
            ws.send_text(json.dumps({"type": "auth", "token": TOKEN}))
            texts = [ws.receive_text() for _ in range(6)]
        # no aircraft here, so no aircraft frame is sent: check the frame itself directly
        texts.append(c.app.state.adsb.tick())
        assert '"type":"aircraft"' in texts[-1]
        assert all(READ_TOKEN not in t for t in texts)
    assert secrets.get() == READ_TOKEN  # reached its own secret store
    assert READ_TOKEN not in s.settings_path.read_text()
    assert "adsb_token" not in json.loads(s.settings_path.read_text())
    assert READ_TOKEN not in caplog.text
    assert all(READ_TOKEN not in f.read_text(errors="ignore")
               for f in tmp_path.rglob("*") if f.is_file())  # file, audit, nothing else


@pytest.mark.parametrize("bad", [
    "https://10.22.123.68/api/v1/ws", "ws://10.22.123.68/api/v1/ws", "10.22.123.68",
    "wss://", "wss://user:pw@10.22.123.68/ws", "wss://host/ws?token=SECRETQ",
    "wss://host/ws#frag", "wss://host/a b",
])
def test_url_rules_refuse_and_never_echo(tmp_path, bad):
    c, _, _ = make_app(tmp_path)
    with c:
        r = c.put("/api/mount/settings", headers=AUTH, json=body(url=bad))
        assert r.status_code == 422
        assert "SECRETQ" not in r.text and "pw@" not in r.text


def test_token_and_range_validation(tmp_path):
    c, secrets, _ = make_app(tmp_path)
    with c:
        for bad in ("with space", "tab\there", "x" * 5000):
            r = c.put("/api/mount/settings", headers=AUTH, json={**body(), "adsb_token": bad})
            assert r.status_code == 422 and bad not in r.text
        for km in (0, 10, 150, 351, "100"):
            assert c.put("/api/mount/settings", headers=AUTH,
                         json=body(range_limit_km=km)).status_code == 422
        for km in (20, 50, 100, 200, 350):
            r = c.put("/api/mount/settings", headers=AUTH, json=body(range_limit_km=km))
            assert r.status_code == 200 and r.json()["adsb"]["range_limit_km"] == km
        assert secrets.get() is None


def test_defaults_and_old_files_and_location_block(tmp_path):
    """A settings.json without an adsb block loads with the defaults; `location` is unchanged."""
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"version": 1, "location": {
        "lat_deg": 14.0, "lon_deg": 100.0, "alt_m": 5.0, "timezone": "UTC"}}))
    st = SettingsStore(p, MemorySecretStore(), MemorySecretStore(), MemorySecretStore())
    a = st.current.adsb
    assert (a.enabled, a.url, a.range_limit_km) == (False, URL, 100)
    assert st.public()["adsb"] == {"enabled": False, "url": URL, "range_limit_km": 100,
                                   "token_set": False}
    st.save(st.current, adsb_token=READ_TOKEN)
    saved = json.loads(p.read_text())
    assert saved["location"] == {"lat_deg": 14.0, "lon_deg": 100.0, "alt_m": 5.0,
                                 "timezone": "UTC"}
    assert saved["adsb"] == {"enabled": False, "url": URL, "range_limit_km": 100}


def test_a_settings_save_restarts_the_runner_with_the_new_values(tmp_path):
    f = fake_adsb.factory()
    c, secrets, _ = make_app(tmp_path, adsb_client_factory=f)
    with c:
        c.put("/api/mount/settings", headers=AUTH,
              json={**body(enabled=True, range_limit_km=50), "adsb_token": READ_TOKEN})
        for _ in range(100):
            if fake_adsb.FakeClient.instances:
                break
            time.sleep(0.05)
        first = fake_adsb.FakeClient.instances[-1]
        assert first.token == READ_TOKEN and first.url == URL
        n = len(fake_adsb.FakeClient.instances)
        newurl = "wss://10.22.123.99/api/v1/ws"
        c.put("/api/mount/settings", headers=AUTH,
              json={**body(enabled=True, url=newurl), "adsb_token": "OTHER-TOKEN-1234567"})
        for _ in range(100):
            if len(fake_adsb.FakeClient.instances) > n:
                break
            time.sleep(0.05)
        second = fake_adsb.FakeClient.instances[-1]
        assert second is not first and second.url == newurl
        assert second.token == "OTHER-TOKEN-1234567"
        # switching it off stops the source
        c.put("/api/mount/settings", headers=AUTH, json=body(enabled=False))
        for _ in range(100):
            if c.get("/api/mount/status").json()["adsb"]["state"] == "disabled":
                break
            time.sleep(0.05)
        assert c.get("/api/mount/status").json()["adsb"]["state"] == "disabled"


def test_ws_gets_the_separate_aircraft_message(tmp_path):
    c, _, _ = make_app(tmp_path, adsb_loader=lambda: (None, "ADS-B module not installed"))
    with c, c.websocket_connect("/api/mount/ws", headers=WS_GOOD) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": TOKEN}))
        msgs = [json.loads(ws.receive_text()) for _ in range(8)]
    types = [m["type"] for m in msgs]
    # no aircraft (module missing): no aircraft frames at all, the status carries the adsb block
    assert types[0] == "hello" and set(types[1:]) == {"status"}
    air = json.loads(c.app.state.adsb.tick())
    assert set(air) == {"type", "seq", "ts", "data"}
    assert air["data"]["aircraft"] == [] and air["data"]["adsb"]["state"] == "module_missing"
    st = next(m for m in msgs if m["type"] == "status")
    assert "aircraft" not in st["data"]["adsb"] and st["data"]["adsb"]["state"] == "module_missing"


# ---------------------------------------------------------------- isolation


def rate_under(tmp_path, adsb_kw, seconds=2.0):
    """Status frames published per second by the real poller while the ADS-B side misbehaves."""

    async def go():
        fake = FakePwi4()
        s = Settings(port=8400, token=TOKEN, settings_path=tmp_path / "s.json",
                     poll_enabled=True, audit_dir=tmp_path / "audit")
        app = create_app(s, transport=fake.transport, secrets=MemorySecretStore(),
                         adsb_secrets=MemorySecretStore(READ_TOKEN), **adsb_kw["app"])
        store = app.state.store
        cur = store.current.model_copy(deep=True)
        cur.adsb.enabled = adsb_kw.get("enabled", True)
        cur.location.timezone = "UTC"
        store.save(cur)
        poller = app.state.poller
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.5)  # source running (flooding), first frames out
            seq0, t0 = poller.seq, time.monotonic()
            gaps = []
            last = t0
            client = app.state.hub.add()
            end = t0 + seconds
            while time.monotonic() < end:
                try:
                    await asyncio.wait_for(client.wake.wait(), 0.5)
                except TimeoutError:
                    pass
                client.wake.clear()
                now = time.monotonic()
                gaps.append(now - last)
                last = now
            dt = time.monotonic() - t0
            src = app.state.adsb._source
            seen = {} if src is None else src.counts()
            return (poller.seq - seq0) / dt, max(gaps), seen

    return asyncio.run(go())


def test_status_rate_holds_while_a_live_source_floods_and_fails(tmp_path):
    async def flood(client):
        client.status("connected")
        bad = {"kind": "decoded", "payload": {"junk": True}}
        good = fake_adsb.overflight_events(seconds=14)
        n = 0
        end = time.monotonic() + 3.0
        while time.monotonic() < end:  # a firehose with a yield per event
            client.on_event(bad if n % 3 else good[n % len(good)])
            n += 1
            if n % 50 == 0:
                client.on_event("not even a dict")
            await asyncio.sleep(0)
        raise RuntimeError(f"boom {READ_TOKEN}")  # then the source dies

    rate, worst_gap, seen = rate_under(
        tmp_path, {"app": {"adsb_client_factory": fake_adsb.factory(flood)}}
    )
    assert seen["invalid"] > 1000 and seen["messages"] > 10  # the firehose really ran
    assert rate > 8.5, rate  # 10 Hz nominal
    assert worst_gap < 0.5


def test_status_rate_holds_while_the_feed_floods(tmp_path):
    rows = [fake_adsb.offset_row(f"F{i:05d}", (i % 40) * 0.5, 5 + (i // 40), 9000.0)
            for i in range(400)]

    async def main():
        fake = FakePwi4()
        async with fake_adsb.FakeFeed(rows, interval=0.0) as feed:
            s = Settings(port=8400, token=TOKEN, settings_path=tmp_path / "s.json",
                         poll_enabled=True, audit_dir=tmp_path / "audit")
            app = create_app(s, transport=fake.transport, secrets=MemorySecretStore(),
                             adsb_secrets=MemorySecretStore(), adsb_simulated=True,
                             adsb_feed_url=feed.url)
            poller = app.state.poller
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.5)
                seq0, t0 = poller.seq, time.monotonic()
                await asyncio.sleep(2.0)
                rate = (poller.seq - seq0) / (time.monotonic() - t0)
                sim_state = app.state.adsb.state
            return rate, sim_state

    rate, sim_state = asyncio.run(main())
    assert sim_state == "simulated"
    assert rate > 8.5, rate


def test_a_hanging_source_does_not_stall_apply_or_the_loop(tmp_path):
    """A client that never returns (and a stop that must cancel it) never blocks a Settings save."""

    async def go():
        hub = Hub()

        async def hang(client):
            client.status("connecting")
            await asyncio.sleep(3600)

        r = AdsbRunner(make_store(tmp_path), hub, simulated=False,
                       client_factory=fake_adsb.factory(hang))
        await r.start()
        t0 = time.monotonic()
        await r.apply()
        await r.apply()
        await r.stop()
        return time.monotonic() - t0

    assert asyncio.run(go()) < 1.0


def test_module_namespace_has_no_top_level_adsb_core_import():
    """The gateway must import with adsb_core absent: only load_core touches it."""
    src = open(adsb_mod.__file__, encoding="utf-8").read()
    top = [ln for ln in src.splitlines() if ln.startswith(("import adsb_core", "from adsb_core"))]
    assert top == []


def test_aircraft_frames_only_while_there_are_aircraft(tmp_path):
    """No aircraft = no frames; frames while there are some; one empty frame when the last goes."""
    c, _, _ = make_app(tmp_path, adsb_loader=lambda: (None, "ADS-B module not installed"))
    runner = c.app.state.adsb
    sent: list[str] = []
    runner.hub = type("Hub", (), {"publish_aux": lambda self, m: sent.append(m)})()
    counts = iter([0, 0, 2, 3, 0, 0, 1])

    def tick(now=None):
        runner._tick_rows = next(counts)
        return f"frame{runner._tick_rows}"

    runner.tick = tick
    for _ in range(7):
        runner._publish_now()
    assert sent == ["frame2", "frame3", "frame0", "frame1"]
