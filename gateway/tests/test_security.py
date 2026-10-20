import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.app import create_app
from gateway.config import Settings

from .conftest import FakePwi4

TOKEN = "t" * 43
GOOD_ORIGIN = "http://127.0.0.1:3200"
WS_GOOD = {"origin": GOOD_ORIGIN, "host": "127.0.0.1:8400"}  # TestClient WS ignores base_url Host


@pytest.fixture()
def client():
    fake = FakePwi4()
    settings = Settings(port=8400, token=TOKEN)
    app = create_app(settings, transport=fake.transport)
    with TestClient(app, base_url="http://127.0.0.1:8400") as c:
        yield c


def test_bad_host_400(client):
    r = client.get("/api/mount/health", headers={"host": "evil.example:8400"})
    assert r.status_code == 400
    r = client.get("/api/mount/status", headers={"host": "127.0.0.1:9999"})
    assert r.status_code == 400
    assert client.get("/api/mount/health", headers={"host": "localhost:8400"}).status_code == 200


def test_status_route(client):
    # lifespan poller fills the first snapshot quickly
    import time

    for _ in range(50):
        r = client.get("/api/mount/status")
        if r.status_code == 200:
            break
        time.sleep(0.05)
    assert r.status_code == 200 and r.json()["state"] == "STOPPED"


def test_session_rules(client):
    assert client.get("/api/mount/session").status_code == 403  # no headers at all
    for h in ({"origin": "http://evil.example"}, {"sec-fetch-site": "cross-site"}):
        assert client.get("/api/mount/session", headers=h).status_code == 403
    r = client.get("/api/mount/session", headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200 and r.json() == {"token": TOKEN}
    assert client.get("/api/mount/session", headers={"origin": GOOD_ORIGIN}).status_code == 200


def test_ws_bad_or_missing_origin_rejected(client):
    for headers in (
        {**WS_GOOD, "origin": "http://evil.example"},
        {"host": "127.0.0.1:8400"},
    ):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/mount/ws", headers=headers):
                pass


def test_ws_bad_host_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/mount/ws", headers={"origin": GOOD_ORIGIN, "host": "evil.example:8400"}
        ):
            pass


@pytest.mark.parametrize("first", [None, {"type": "auth", "token": "wrong"}, {"type": "x"}, "junk"])
def test_ws_bad_auth_4401(client, first):
    with client.websocket_connect("/api/mount/ws", headers=WS_GOOD) as ws:
        if first is not None:
            ws.send_text(first if isinstance(first, str) else json.dumps(first))
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
        assert ei.value.code == 4401


def test_ws_good_hello_then_status(client):
    with client.websocket_connect("/api/mount/ws", headers=WS_GOOD) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": TOKEN}))
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello" and set(hello) == {"type", "seq", "ts", "data"}
        m1 = json.loads(ws.receive_text())
        m2 = json.loads(ws.receive_text())
        assert m1["type"] == m2["type"] == "status"
        assert m2["seq"] > m1["seq"] and m2["data"]["state"] == "STOPPED"
