from fastapi.testclient import TestClient

from analysis.app import app


def test_health() -> None:
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "analysis", "version": "0.1.0"}
