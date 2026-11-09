"""T5.2b: camera_pixel_um setting + POST /api/mount/astrometry/run relay (fake analysis)."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.settings import MemorySecretStore, SettingsStore, StationSettings

from .conftest import FakePwi4
from .test_settings import AUTH, body

TOKEN = "t" * 43
KEY = "SECRET-ASTROMETRY-KEY-123"
from gateway.app import ASTROMETRY_BAD_REQUEST, ASTROMETRY_NO_KEY  # noqa: E402

URL = "/api/mount/astrometry/run"


class FakeAnalysis:
    def __init__(self, status=202, payload=None, raise_exc=None, text=None):
        self.status, self.raise_exc, self.text = status, raise_exc, text
        self.payload = payload if payload is not None else {"job_id": "j1", "state": "uploading"}
        self.requests: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sent(self) -> dict:
        return json.loads(self.requests[-1].content)


def make(tmp_path, analysis, key=KEY):
    secrets = MemorySecretStore(key)
    settings = Settings(
        port=8400, token=TOKEN, settings_path=tmp_path / "s.json", poll_enabled=False
    )
    app = create_app(
        settings,
        transport=FakePwi4().transport,
        secrets=secrets,
        analysis_transport=analysis.transport,
    )
    return TestClient(app, base_url="http://127.0.0.1:8400"), app


def web_body(**over):
    b = {"image_id": "img1", "detections": [{"id": "T", "role": "target"}],
         "epoch_iso": "2025-04-04T13:01:30Z", "exposure_s": 10}
    b.update(over)
    return b


# ------------------------------------------------------------------ settings


def test_camera_pixel_um_defaults_null_and_round_trips(tmp_path):
    a = FakeAnalysis()
    c, app = make(tmp_path, a)
    assert c.get("/api/mount/settings", headers=AUTH).json()["hardware"]["camera_pixel_um"] is None
    r = c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": 3.76}))
    assert r.status_code == 200 and r.json()["hardware"]["camera_pixel_um"] == 3.76
    assert c.get("/api/mount/settings", headers=AUTH).json()["hardware"]["camera_pixel_um"] == 3.76
    # persisted in the file, reloaded by a new store
    reloaded = SettingsStore(tmp_path / "s.json", MemorySecretStore())
    assert reloaded.current.hardware.camera_pixel_um == 3.76
    # null clears it
    r = c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": None}))
    assert r.json()["hardware"]["camera_pixel_um"] is None


def test_camera_pixel_um_range_and_omitted_keeps(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis())
    for bad in (0.4, 30.1, -1):
        r = c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": bad}))
        assert r.status_code == 422
    for ok in (0.5, 30):
        r = c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": ok}))
        assert r.status_code == 200
    c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": 5.5}))
    b = body()
    del b["hardware"]["camera_pixel_um"]
    r = c.put("/api/mount/settings", headers=AUTH, json=b)
    assert r.json()["hardware"]["camera_pixel_um"] == 5.5


def test_old_settings_file_without_the_field_loads():
    s = StationSettings.model_validate({"hardware": {"mount": "x"}})
    assert s.hardware.camera_pixel_um is None


# ------------------------------------------------------------------ relay


def test_relay_requires_auth(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis())
    assert c.post(URL, json=web_body()).status_code == 401


def test_relay_injects_key_and_fills_hints(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a)
    c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": 3.76}))
    r = c.post(URL, headers=AUTH, json=web_body())
    assert r.status_code == 202 and r.json() == {"job_id": "j1", "state": "uploading"}
    assert str(a.requests[-1].url).endswith("/api/astrometry/run")
    sent = a.sent()
    assert sent["apikey"] == KEY
    assert sent["focal_mm"] == 620.0 and sent["pixel_um"] == 3.76
    assert sent["image_id"] == "img1" and sent["exposure_s"] == 10
    assert KEY not in r.text


def test_relay_keeps_explicit_hints_and_ignores_a_client_key(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a)
    c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": 3.76}))
    c.post(URL, headers=AUTH, json=web_body(focal_mm=1000, pixel_um=5, apikey="client-key"))
    sent = a.sent()
    assert sent["focal_mm"] == 1000 and sent["pixel_um"] == 5 and sent["apikey"] == KEY


def test_relay_blind_when_pixel_unknown(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a)
    c.post(URL, headers=AUTH, json=web_body())
    assert a.sent()["pixel_um"] is None and a.sent()["focal_mm"] == 620.0


def test_relay_station_source_fills_and_always_adds_station_optics(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a)
    c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": 3.76}))
    c.post(URL, headers=AUTH, json=web_body(source="station"))
    sent = a.sent()
    assert sent["source"] == "station"
    assert sent["focal_mm"] == 620.0 and sent["pixel_um"] == 3.76
    assert sent["station_focal_mm"] == 620.0 and sent["station_pixel_um"] == 3.76
    c.post(URL, headers=AUTH, json=web_body())   # default source
    assert a.sent()["source"] == "station"


def test_relay_other_source_is_never_filled_from_the_station(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a)
    c.put("/api/mount/settings", headers=AUTH, json=body(hardware={"camera_pixel_um": 3.76}))
    r = c.post(URL, headers=AUTH, json=web_body(source="other"))
    assert r.status_code == 202
    sent = a.sent()
    assert sent["source"] == "other" and sent["focal_mm"] is None and sent["pixel_um"] is None
    assert sent["station_focal_mm"] == 620.0 and sent["station_pixel_um"] == 3.76
    assert sent["apikey"] == KEY and KEY not in r.text
    c.post(URL, headers=AUTH, json=web_body(source="other", focal_mm=1000, pixel_um=5,
                                            station_focal_mm=1, station_pixel_um=1))
    sent = a.sent()
    assert sent["focal_mm"] == 1000 and sent["pixel_um"] == 5   # the file's own values
    assert sent["station_focal_mm"] == 620.0 and sent["station_pixel_um"] == 3.76


def test_relay_station_optics_pixel_null_when_unknown(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a)
    c.post(URL, headers=AUTH, json=web_body(source="other"))
    assert a.sent()["station_pixel_um"] is None and a.sent()["station_focal_mm"] == 620.0


def test_relay_rejects_unknown_source(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis())
    assert c.post(URL, headers=AUTH, json=web_body(source="x")).status_code == 422


def test_relay_no_key_409_and_nothing_sent(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a, key=None)
    r = c.post(URL, headers=AUTH, json=web_body())
    assert r.status_code == 409
    assert r.json()["detail"] == "Set the astrometry.net API key in Station Settings"
    assert a.requests == []


def test_relay_analysis_down_503(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis(raise_exc=httpx.ConnectError(f"refused {KEY}")))
    r = c.post(URL, headers=AUTH, json=web_body())
    assert r.status_code == 503 and r.json()["detail"] == "Analysis service unavailable"
    assert KEY not in r.text


def test_relay_non_json_analysis_is_503(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis(status=502, text="<html>bad gateway</html>"))
    r = c.post(URL, headers=AUTH, json=web_body())
    assert r.status_code == 503 and "html" not in r.text


def test_relay_passes_analysis_status_and_json(tmp_path):
    for status, payload in ((409, {"detail": "An astrometry solve is already running"}),
                            (422, {"detail": "Mark the target before running"}),
                            (404, {"detail": "Image expired — load it again"})):
        c, _ = make(tmp_path, FakeAnalysis(status=status, payload=payload))
        r = c.post(URL, headers=AUTH, json=web_body())
        assert r.status_code == status and r.json() == payload


def test_relay_validates_web_body(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis())
    assert c.post(URL, headers=AUTH, json={"epoch_iso": "x"}).status_code == 422


def test_key_never_in_logs_audit_or_responses(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    ok = FakeAnalysis()
    c, app = make(tmp_path, ok)
    outs = [c.post(URL, headers=AUTH, json=web_body()).text,
            c.get("/api/mount/settings", headers=AUTH).text]
    bad = FakeAnalysis(raise_exc=httpx.ConnectError(f"boom {KEY}"))
    c2, app2 = make(tmp_path, bad)
    outs.append(c2.post(URL, headers=AUTH, json=web_body()).text)
    assert KEY not in "\n".join(outs)
    assert KEY not in caplog.text
    audit_dir = app.state.audit.dir
    lines = "".join(f.read_text(encoding="utf-8") for f in audit_dir.glob("*.jsonl"))
    assert "astrometry_run" in lines and KEY not in lines and "apikey" not in lines


def test_an_echoed_key_is_never_passed_on(tmp_path):
    # a FastAPI validation error can echo its whole input; the relay must not pass the key on
    for status, payload in ((422, {"detail": [{"msg": "bad", "input": {"apikey": KEY}}]}),
                            (422, {"detail": [{"msg": "bad", "input": "x"}]}),
                            (500, {"detail": f"oops {KEY}"})):
        c, _ = make(tmp_path, FakeAnalysis(status=status, payload=payload))
        r = c.post(URL, headers=AUTH, json=web_body())
        assert r.status_code == status
        assert KEY not in r.text
        assert r.json() == {"detail": ASTROMETRY_BAD_REQUEST}


# ------------------------------------------------------------------ resume relay

RESUME = "/api/mount/astrometry/job/abc123/resume"


def test_resume_requires_auth(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis())
    assert c.post(RESUME).status_code == 401


def test_resume_injects_key_and_relays(tmp_path):
    a = FakeAnalysis()
    c, app = make(tmp_path, a)
    r = c.post(RESUME, headers=AUTH)
    assert r.status_code == 202 and r.json() == {"job_id": "j1", "state": "uploading"}
    assert str(a.requests[-1].url).endswith("/api/astrometry/job/abc123/resume")
    assert a.sent() == {"apikey": KEY}
    assert KEY not in r.text
    lines = "".join(f.read_text(encoding="utf-8") for f in app.state.audit.dir.glob("*.jsonl"))
    assert "astrometry_resume" in lines and KEY not in lines and "apikey" not in lines


def test_resume_no_key_409_and_nothing_sent(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a, key=None)
    r = c.post(RESUME, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"] == ASTROMETRY_NO_KEY
    assert a.requests == []


def test_resume_analysis_down_503_and_non_json(tmp_path):
    c, _ = make(tmp_path, FakeAnalysis(raise_exc=httpx.ConnectError(f"refused {KEY}")))
    r = c.post(RESUME, headers=AUTH)
    assert r.status_code == 503 and r.json()["detail"] == "Analysis service unavailable"
    assert KEY not in r.text
    c, _ = make(tmp_path, FakeAnalysis(status=502, text="<html>bad</html>"))
    assert c.post(RESUME, headers=AUTH).status_code == 503


def test_resume_passes_status_and_scrubs_echoes(tmp_path):
    for status, payload in ((409, {"detail": "An astrometry solve is already running"}),
                            (422, {"detail": "This astrometry job cannot be resumed"}),
                            (404, {"detail": "Unknown astrometry job"})):
        c, _ = make(tmp_path, FakeAnalysis(status=status, payload=payload))
        r = c.post(RESUME, headers=AUTH)
        assert r.status_code == status and r.json() == payload
    for status, payload in ((422, {"detail": [{"msg": "bad", "input": {"apikey": KEY}}]}),
                            (422, {"detail": [{"msg": "bad", "input": "x"}]}),
                            (500, {"detail": f"oops {KEY}"})):
        c, _ = make(tmp_path, FakeAnalysis(status=status, payload=payload))
        r = c.post(RESUME, headers=AUTH)
        assert r.status_code == status and KEY not in r.text
        assert r.json() == {"detail": ASTROMETRY_BAD_REQUEST}


def test_resume_job_id_is_path_escaped(tmp_path):
    a = FakeAnalysis()
    c, _ = make(tmp_path, a)
    c.post("/api/mount/astrometry/job/a%3Fb/resume", headers=AUTH)
    assert "/job/a%3Fb/resume" in str(a.requests[-1].url)
