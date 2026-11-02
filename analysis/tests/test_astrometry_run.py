"""T5.2b: nova client (fake), object solve mapping, job registry, /run + /job API. No network."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from analysis.app import create_app
from analysis.astrometry import api as astro_api
from analysis.astrometry import jobs as jobs_mod
from analysis.astrometry import nova
from analysis.astrometry import solve as solve_mod
from analysis.astrometry.io import ImageData

KEY = "SECRET-NOVA-KEY-ABC123"
W, H = 400, 300
EPOCH = "2025-04-04T13:01:30.000Z"


class ScriptedNova:
    """Fake nova: optional gate to hold the solve, or an error / timeout to raise."""

    def __init__(self, fail: str | None = None, scale: float | None = None,
                 gate: threading.Event | None = None):
        self.fail, self.scale, self.gate = fail, scale, gate
        self.calls: list[dict] = []

    def solve(self, image_path, *, width, height, scale_est_arcsec, timeout=300,
              progress=None, check_cancel=None):
        self.calls.append({"w": width, "h": height, "scale": scale_est_arcsec,
                           "timeout": timeout})
        progress(stage="uploading")
        progress(stage="queued", subid=7)
        progress(stage="solving", jobid=9)
        if self.gate is not None:
            while not self.gate.wait(0.02):
                check_cancel()
        if self.fail:
            raise nova.NovaError(self.fail)
        s = self.scale or scale_est_arcsec or 5.0
        d = s / 3600.0
        hdr = {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": 150.0, "CRVAL2": 20.0,
               "CRPIX1": width / 2 + 0.5, "CRPIX2": height / 2 + 0.5,
               "CD1_1": -d, "CD1_2": 0.0, "CD2_1": 0.0, "CD2_2": d}
        return nova.NovaSolution(150.0, 20.0, 0.0, s, -1, hdr)


def _dets(n_streaks: int = 12, target: bool = True) -> list[dict]:
    out = []
    if target:
        out.append({"id": "T", "role": "target", "shape": "point", "x": 250.5, "y": 100.5,
                    "xs": None, "ys": None, "xe": None, "ye": None, "brt": 3})
    for i in range(n_streaks):
        x, y = 30 + 25 * i, 40 + 15 * i
        out.append({"id": f"S{i + 1}", "role": "star", "shape": "streak", "x": x + 5, "y": y,
                    "xs": x, "ys": y, "xe": x + 10, "ye": y, "brt": 2})
    return out


def _wait(client, job_id, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/astrometry/job/{job_id}").json()
        if j["state"] not in jobs_mod.ACTIVE_STATES:
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture()
def env():
    app = create_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    fake = ScriptedNova()
    app.state.nova_factory = lambda key: fake
    img = ImageData(data=np.zeros((H, W), np.float32), width=W, height=H, source_format="fits")
    image_id = app.state.astrometry.add(img, {"dims": [W, H]})
    with TestClient(app) as c:
        yield c, app, fake, image_id


def _body(image_id, **over):
    b = {"image_id": image_id, "detections": _dets(), "epoch_iso": EPOCH, "exposure_s": 10.0,
         "focal_mm": 620.0, "pixel_um": 3.76, "apikey": KEY}
    b.update(over)
    return b


# -- mapping math -------------------------------------------------------------
def test_map_point_known_pixel_to_known_radec():
    d = 5.0 / 3600
    hdr = {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": 150.0, "CRVAL2": 0.0,
           "CRPIX1": 201.0, "CRPIX2": 151.0, "CD1_1": -d, "CD1_2": 0, "CD2_1": 0, "CD2_2": d}
    wcs = solve_mod.build_wcs(hdr)
    # CRPIX is 1-indexed (201, 151) = 0-indexed (200, 150): the reference pixel
    ra, dec = solve_mod.map_point(wcs, 200, 150)
    assert (ra, dec) == pytest.approx((150.0, 0.0), abs=1e-9)
    # 100 px right = 500" east-of... RA decreases with x (CD1_1 < 0); 60 px up = +300" dec
    ra, dec = solve_mod.map_point(wcs, 300, 210)
    assert ra == pytest.approx(150.0 - 500.0 / 3600, abs=1e-6)
    assert dec == pytest.approx(300.0 / 3600, abs=1e-6)


def test_mid_exposure_epoch_and_warning():
    iso, unix, w = solve_mod.mid_exposure("2025-04-04T13:01:30.000Z", 10.0)
    assert iso == "2025-04-04T13:01:35.000Z" and w == []
    assert unix == pytest.approx(1743771695.0)
    iso, unix, w = solve_mod.mid_exposure("2025-04-04T13:01:30.500Z", None)
    assert iso == "2025-04-04T13:01:30.500Z" and len(w) == 1
    assert solve_mod.mid_exposure("2025-04-04T13:01:30Z", 0)[2]


def test_hint_scale_and_scale_check():
    assert solve_mod.hint_scale_arcsec(620, 3.76) == pytest.approx(1.2510, abs=1e-3)
    assert solve_mod.hint_scale_arcsec(None, 3.76) is None
    assert solve_mod.scale_ok(1.3, 1.25) and not solve_mod.scale_ok(2.5, 1.25)
    assert solve_mod.scale_ok(99.0, None)


def test_synthetic_field_has_a_peak_at_each_star():
    img = solve_mod.render_synthetic_points([(50.0, 40.0), (120.0, 90.0)], 200, 150)
    assert img[40, 50] > 10000 and img[90, 120] > 10000 and img[5, 5] < 200


# -- the fake nova -------------------------------------------------------------
def test_fake_nova_wcs_from_image_size_and_cancel():
    f = nova.FakeNova(delay_s=0.0)
    sol = f.solve("x", width=100, height=60, scale_est_arcsec=None)
    wcs = solve_mod.build_wcs(sol.wcs_header)
    ra, dec = solve_mod.map_point(wcs, 49.5, 29.5)   # image centre
    assert (ra, dec) == pytest.approx(nova.FAKE_CENTER, abs=1e-6)

    def boom():
        raise jobs_mod.JobCancelled()
    with pytest.raises(jobs_mod.JobCancelled):
        nova.FakeNova(delay_s=1.0).solve("x", width=1, height=1, scale_est_arcsec=None,
                                         check_cancel=boom)


# -- /run validation -----------------------------------------------------------
def test_run_requires_apikey(env):
    c, _, _, iid = env
    r = c.post("/api/astrometry/run", json=_body(iid, apikey=None))
    assert r.status_code == 422 and r.json()["detail"] == "API key missing"
    b = _body(iid)
    del b["apikey"]
    assert c.post("/api/astrometry/run", json=b).json()["detail"] == "API key missing"


def test_run_unknown_image_is_404(env):
    c, *_ = env
    r = c.post("/api/astrometry/run", json=_body("nope"))
    assert r.status_code == 404 and r.json()["detail"] == astro_api.EXPIRED


@pytest.mark.parametrize("dets,over,text", [
    (_dets(9), {}, "At least 10 complete star streaks are needed (9 found)"),
    (_dets(12, target=False), {}, "Mark the target before running"),
    (None, {"epoch_iso": None}, "Enter the exposure start time"),
    (None, {"epoch_iso": "yesterday"}, "Enter the exposure start time"),
])
def test_run_validation_422(env, dets, over, text):
    c, *_ = env
    b = _body(env[3], **over)
    if dets is not None:
        b["detections"] = dets
    r = c.post("/api/astrometry/run", json=b)
    assert r.status_code == 422 and r.json()["detail"] == text


def test_incomplete_streaks_do_not_count(env):
    c, _, _, iid = env
    dets = _dets(12)
    for d in dets[-3:]:
        d["xe"] = None                       # 3 incomplete -> 9 complete
    r = c.post("/api/astrometry/run", json=_body(iid, detections=dets))
    assert r.status_code == 422 and "(9 found)" in r.json()["detail"]


# -- the state machine ----------------------------------------------------------
def test_job_done_result_shape_and_states(env):
    c, app, fake, iid = env
    gate = threading.Event()
    fake.gate = gate
    r = c.post("/api/astrometry/run", json=_body(iid))
    assert r.status_code == 202
    jid = r.json()["job_id"]
    assert r.json()["state"] == "uploading"
    # while the solve is held: solving, reattach via /job
    for _ in range(100):
        j = c.get(f"/api/astrometry/job/{jid}").json()
        if j["state"] == "solving":
            break
        time.sleep(0.02)
    assert j["state"] == "solving" and j["stage_text"] and "result" not in j
    assert c.get("/api/astrometry/job").json()["job_id"] == jid
    gate.set()
    j = _wait(c, jid)
    assert j["state"] == "done" and "error" not in j
    res = j["result"]
    assert set(res) == {"ra_deg", "dec_deg", "epoch_mid_iso", "epoch_mid_unix",
                        "residual_arcsec", "scale_arcsec_px", "n_stars_used", "warnings"}
    assert res["scale_arcsec_px"] == pytest.approx(1.251, abs=1e-3)
    assert res["epoch_mid_iso"] == "2025-04-04T13:01:35.000Z"
    assert res["n_stars_used"] == 12 and res["warnings"] == []
    assert fake.calls[0]["scale"] == pytest.approx(1.251, abs=1e-3)
    assert fake.calls[0]["timeout"] == 1800.0
    # finished job is returned by GET /job (latest), not 404
    assert c.get("/api/astrometry/job").json()["state"] == "done"


def test_result_matches_independent_wcs_math(env):
    c, _, _, iid = env
    jid = c.post("/api/astrometry/run", json=_body(iid)).json()["job_id"]
    res = _wait(c, jid)["result"]
    s = res["scale_arcsec_px"]
    d = 1.251 / 3600   # scale actually used by the fake = the hint
    # pixel (250.5, 100.5); reference 0-indexed pixel (200.0, 150.0)
    assert res["dec_deg"] == pytest.approx(20.0 + (100.5 - 150.0) * d, abs=2e-4)
    assert 150.0 - res["ra_deg"] > 0 and s == pytest.approx(1.251, abs=1e-3)


def test_blind_solve_when_no_hint(env):
    c, _, fake, iid = env
    jid = c.post("/api/astrometry/run", json=_body(iid, focal_mm=None, pixel_um=None)
                 ).json()["job_id"]
    assert _wait(c, jid)["state"] == "done"
    assert fake.calls[0]["scale"] is None


def test_no_exposure_gives_warning_and_epoch(env):
    c, _, _, iid = env
    jid = c.post("/api/astrometry/run", json=_body(iid, exposure_s=None)).json()["job_id"]
    res = _wait(c, jid)["result"]
    assert res["epoch_mid_iso"] == EPOCH and len(res["warnings"]) == 1


@pytest.mark.parametrize("kind,state,text", [
    (nova.FAILED, "failed", "astrometry.net could not solve this frame"),
    (nova.UNREACHABLE, "failed", "astrometry.net unreachable (network)"),
    (nova.INVALID_KEY, "failed", "Invalid astrometry.net API key"),
    (nova.TIMED_OUT, "timeout", "Solve timed out"),
])
def test_job_errors(env, kind, state, text):
    c, _, fake, iid = env
    fake.fail = kind
    jid = c.post("/api/astrometry/run", json=_body(iid)).json()["job_id"]
    j = _wait(c, jid)
    assert j["state"] == state and j["error"] == text and "result" not in j


def test_scale_mismatch_fails(env):
    c, _, fake, iid = env
    fake.scale = 3.0   # hint is 1.25 "/px
    jid = c.post("/api/astrometry/run", json=_body(iid)).json()["job_id"]
    j = _wait(c, jid)
    assert j["state"] == "failed" and j["error"] == "Solved scale does not match the camera"


def test_scale_mismatch_ignored_when_blind(env):
    c, _, fake, iid = env
    fake.scale = 3.0
    jid = c.post("/api/astrometry/run", json=_body(iid, focal_mm=None, pixel_um=None)
                 ).json()["job_id"]
    assert _wait(c, jid)["state"] == "done"


def test_unexpected_exception_is_readable_not_leaked(env):
    c, app, _, iid = env

    class Boom:
        def solve(self, *a, **k):
            raise RuntimeError(f"secret detail {KEY}")
    app.state.nova_factory = lambda key: Boom()
    jid = c.post("/api/astrometry/run", json=_body(iid)).json()["job_id"]
    j = _wait(c, jid)
    assert j["state"] == "failed" and KEY not in str(j) and "secret detail" not in str(j)


def test_single_active_409_then_free_after_finish(env):
    c, _, fake, iid = env
    gate = threading.Event()
    fake.gate = gate
    first = c.post("/api/astrometry/run", json=_body(iid))
    assert first.status_code == 202
    second = c.post("/api/astrometry/run", json=_body(iid))
    assert second.status_code == 409
    assert second.json()["detail"] == "An astrometry solve is already running"
    gate.set()
    _wait(c, first.json()["job_id"])
    assert c.post("/api/astrometry/run", json=_body(iid)).status_code == 202


def test_cancel(env):
    c, _, fake, iid = env
    fake.gate = threading.Event()   # never set: only cancel ends it
    jid = c.post("/api/astrometry/run", json=_body(iid)).json()["job_id"]
    r = c.post(f"/api/astrometry/job/{jid}/cancel")
    assert r.status_code == 200
    assert _wait(c, jid)["state"] == "cancelled"
    assert c.post("/api/astrometry/job/unknown/cancel").status_code == 404


def test_job_get_404s(env):
    c, *_ = env
    assert c.get("/api/astrometry/job/nope").status_code == 404
    assert c.get("/api/astrometry/job").status_code == 404   # nothing yet


def test_latest_prefers_active_over_finished(env):
    c, _, fake, iid = env
    first = c.post("/api/astrometry/run", json=_body(iid)).json()["job_id"]
    _wait(c, first)
    fake.gate = threading.Event()
    second = c.post("/api/astrometry/run", json=_body(iid)).json()["job_id"]
    assert c.get("/api/astrometry/job").json()["job_id"] == second
    c.post(f"/api/astrometry/job/{second}/cancel")
    _wait(c, second)
    assert c.get("/api/astrometry/job").json()["job_id"] == second   # newest finished


# -- the key never leaks ----------------------------------------------------------
def test_key_not_in_any_job_output_or_log(env, caplog):
    c, app, fake, iid = env
    caplog.set_level(logging.DEBUG)
    outs = []
    for kind in (None, nova.FAILED, nova.INVALID_KEY):
        fake.fail = kind
        r = c.post("/api/astrometry/run", json=_body(iid))
        outs.append(r.text)
        jid = r.json()["job_id"]
        outs.append(str(_wait(c, jid)))
        outs.append(c.get("/api/astrometry/job").text)
        outs.append(str(app.state.astro_jobs.get(jid).__dict__))
    assert KEY not in "\n".join(outs)
    assert KEY not in caplog.text


def test_fake_env_switch_installs_fake(monkeypatch):
    monkeypatch.setenv("PW_ASTROMETRY_FAKE", "1")
    app = create_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert app.state.nova_factory is nova.fake_factory
    monkeypatch.delenv("PW_ASTROMETRY_FAKE")
    app = create_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert app.state.nova_factory is nova.real_factory


def test_assembled_app_image_then_run_share_one_session_store(monkeypatch):
    """/image and /run must see the same image store in the real create_app() wiring (a stale
    duplicate router once registered /image on one store and /run on another -> 'Image expired')."""
    import io as _io

    import numpy as _np
    from fastapi.testclient import TestClient as _TC
    from PIL import Image

    from analysis.app import create_app

    monkeypatch.setenv("PW_ASTROMETRY_FAKE", "1")
    buf = _io.BytesIO()
    px = (_np.random.default_rng(1).random((64, 96)) * 255).astype("uint8")
    Image.fromarray(px).save(buf, "PNG")
    with _TC(create_app()) as c:
        r = c.post("/api/astrometry/image", files={"file": ("f.png", buf.getvalue(), "image/png")})
        assert r.status_code == 200
        image_id = r.json()["image_id"]
        dets = [{"id": "T", "role": "target", "shape": "point", "x": 48.0, "y": 32.0,
                 "xs": None, "ys": None, "xe": None, "ye": None, "brt": 3}]
        dets += [{"id": f"S{i}", "role": "star", "shape": "streak", "x": 10.0 + i, "y": 5.0 + 2 * i,
                  "xs": 5.0 + i, "ys": 2.0 + 2 * i, "xe": 15.0 + i, "ye": 8.0 + 2 * i, "brt": 3}
                 for i in range(1, 13)]
        body = {"image_id": image_id, "detections": dets, "epoch_iso": "2025-04-04T13:01:30Z",
                "exposure_s": 2, "apikey": "dummy"}
        r = c.post("/api/astrometry/run", json=body)
        assert r.status_code == 202, r.text
