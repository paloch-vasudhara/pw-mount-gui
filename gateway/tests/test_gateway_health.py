from fastapi.testclient import TestClient

from gateway.app import app


def test_health() -> None:
    r = TestClient(app).get("/api/mount/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "gateway", "version": "0.1.0"}
