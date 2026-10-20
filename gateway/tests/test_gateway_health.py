from fastapi.testclient import TestClient

from gateway.app import app


def test_health() -> None:
    # Host check (I7) means the client must present a loopback Host.
    r = TestClient(app, base_url="http://127.0.0.1:8400").get("/api/mount/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "gateway", "version": "0.1.0"}
