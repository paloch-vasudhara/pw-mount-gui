"""T5.2a: metadata, image sessions, /api/astrometry image+detect+snap, real OW0021 frames."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from analysis.app import create_app
from analysis.astrometry import api as astro_api
from analysis.astrometry import service
from analysis.astrometry.io import ImageData
from analysis.astrometry.sessions import ImageSessions
from analysis.tests.fixture_catalog import factory_for  # noqa: F401
from analysis.tests.test_astrometry_select import _object_frame

SSA_DATA = Path("C:/Users/soar/Desktop/DELV/ssa-toolkit/examples/data/astrometry")


def _img(**kw) -> ImageData:
    base = {"data": np.zeros((4, 6), np.float32), "width": 6, "height": 4, "source_format": "fits"}
    base.update(kw)
    return ImageData(**base)


# -- metadata parse --------------------------------------------------------
def test_epoch_normalisation():
    n = service.normalize_epoch
    assert n("2025-04-04T13:01:30.123") == "2025-04-04T13:01:30.123Z"
    assert n("2025-04-04 13:01:30") == "2025-04-04T13:01:30Z"
    assert n("2022-01-14 08:36:00.65") == "2022-01-14T08:36:00.65Z"
    assert n("garbage") is None and n(None) is None and n("") is None


def test_meta_fits_header_fields():
    m = service.build_meta(_img(
        epoch="2025-04-04T13:01:30", exposure_s=2.5, bit_depth=16,
        header={"FOCALLEN": 620.0, "XPIXSZ": 3.76}))
    assert m["format"] == "fits" and m["bit_depth"] == 16 and m["dims"] == [6, 4]
    assert m["epoch_iso"] == "2025-04-04T13:01:30Z"
    assert (m["exposure_s"], m["focal_mm"], m["pixel_um"]) == (2.5, 620.0, 3.76)
    assert m["warnings"] == []


def test_meta_missing_and_invalid_fields_are_null():
    m = service.build_meta(_img(source_format="tiff", header={"FOCALLEN": "x", "XPIXSZ": -1}))
    assert m["epoch_iso"] is None and m["exposure_s"] is None
    assert m["focal_mm"] is None and m["pixel_um"] is None
    m = service.build_meta(_img(source_format="tiff", epoch="2025-04-04 13:01:30"))
    assert m["epoch_iso"] == "2025-04-04T13:01:30Z"
    assert any("UTC" in w for w in m["warnings"])
    m = service.build_meta(_img(epoch="not a date"))
    assert m["epoch_iso"] is None and m["warnings"]


def test_preview_downscales_to_1600():
    p = service.make_preview(np.random.default_rng(0).random((800, 4000)).astype(np.float32))
    assert (p["width"], p["height"]) == (1600, 320) and p["scale"] == pytest.approx(0.4)
    assert p["png_b64"].startswith("data:image/png;base64,")
    small = service.make_preview(np.zeros((30, 40), np.float32))
    assert (small["width"], small["height"], small["scale"]) == (40, 30, 1.0)


# -- sessions --------------------------------------------------------------
class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_sessions_evict_oldest_beyond_three():
    clk = Clock()
    s = ImageSessions(clock=clk)
    ids = []
    for _ in range(3):
        ids.append(s.add(_img(), {}))
        clk.t += 1
    s.get(ids[0])                 # touch -> ids[1] is now the least recently used
    clk.t += 1
    ids.append(s.add(_img(), {}))
    assert len(s) == 3
    assert s.get(ids[1]) is None
    assert all(s.get(i) is not None for i in (ids[0], ids[2], ids[3]))
    assert len(set(ids)) == 4 and all(len(i) == 32 for i in ids)


def test_sessions_idle_ttl_with_fake_clock():
    clk = Clock()
    s = ImageSessions(clock=clk)
    a = s.add(_img(), {})
    clk.t += 29 * 60
    assert s.get(a) is not None          # a use restarts the idle timer
    clk.t += 29 * 60
    assert s.get(a) is not None
    clk.t += 30 * 60 + 1
    assert s.get(a) is None
    assert len(s) == 0


# -- endpoints, synthetic frame ---------------------------------------------
@pytest.fixture()
def client():
    app = create_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


def _fits_bytes(data, **hdr) -> bytes:
    from astropy.io import fits

    h = fits.PrimaryHDU(np.clip(data + 1000, 0, 65535).astype(np.uint16))
    for k, v in hdr.items():
        h.header[k.replace("_", "-") if k == "DATE_OBS" else k] = v
    buf = io.BytesIO()
    h.writeto(buf)
    return buf.getvalue()


def _upload(client, name, content):
    return client.post("/api/astrometry/image", files={"file": (name, content)})


def test_image_detect_snap_roundtrip(client):
    data, centers, tgt = _object_frame(n_stars=8, seed=2)
    blob = _fits_bytes(data, DATE_OBS="2025-04-04T13:01:30", EXPTIME=2.0,
                       FOCALLEN=620.0, XPIXSZ=3.76)
    r = _upload(client, "frame.fits", blob)
    assert r.status_code == 200, r.text
    j = r.json()
    assert len(j["image_id"]) == 32
    assert j["meta"] == {
        "format": "fits", "bit_depth": 16, "dims": [400, 320],
        "epoch_iso": "2025-04-04T13:01:30Z", "exposure_s": 2.0,
        "focal_mm": 620.0, "pixel_um": 3.76, "warnings": [],
    }
    pv = j["preview"]
    assert (pv["width"], pv["height"], pv["scale"]) == (400, 320, 1.0)
    png = base64.b64decode(pv["png_b64"].split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    d = client.post("/api/astrometry/detect", json={"image_id": j["image_id"]})
    assert d.status_code == 200, d.text
    dj = d.json()
    assert set(dj) == {"detections", "n_streaks", "target", "warnings"}
    assert dj["n_streaks"] >= 6
    t = dj["target"]
    assert t["role"] == "target" and t["shape"] == "point"
    assert np.hypot(t["x"] - tgt[0], t["y"] - tgt[1]) < 2.0
    assert t["xs"] is None and t["xe"] is None
    stars = [x for x in dj["detections"] if x["role"] == "star"]
    assert len(stars) == dj["n_streaks"]
    assert all(s["shape"] == "streak" and None not in (s["xs"], s["ys"], s["xe"], s["ye"])
               for s in stars)
    assert all(1 <= s["brt"] <= 5 for s in dj["detections"])
    # T5.2b helpers
    assert len(service.star_midpoints(dj["detections"])) == len(stars)
    assert service.target_point(dj["detections"]) == (t["x"], t["y"])

    # snap a click 3 px from the target onto it
    s = client.post("/api/astrometry/snap", json={
        "image_id": j["image_id"], "x": tgt[0] + 3, "y": tgt[1] - 2, "role": "target"})
    assert s.status_code == 200, s.text
    sj = s.json()
    assert sj["role"] == "target" and sj["shape"] == "point"
    assert np.hypot(sj["x"] - tgt[0], sj["y"] - tgt[1]) < 2.0


def test_png_upload_and_temp_file_removed(client, monkeypatch):
    from PIL import Image

    seen = []
    real = astro_api._unlink
    monkeypatch.setattr(astro_api, "_unlink", lambda p: (seen.append(p), real(p)))
    buf = io.BytesIO()
    Image.fromarray(np.full((20, 30), 40, np.uint8)).save(buf, format="PNG")
    r = _upload(client, "x.PNG", buf.getvalue())
    assert r.status_code == 200 and r.json()["meta"]["format"] == "png"
    assert seen and not os.path.exists(seen[0])


def test_image_rejections(client, monkeypatch):
    r = _upload(client, "notes.txt", b"hello")
    assert r.status_code == 422 and r.json()["detail"] == "Unsupported file type"
    r = _upload(client, "a.fits", b"")
    assert r.status_code == 422
    r = _upload(client, "bad.tif", b"not an image at all")
    assert r.status_code == 422
    assert r.json()["detail"] == "Could not read this image"
    monkeypatch.setattr(astro_api, "MAX_UPLOAD_BYTES", 100)
    r = _upload(client, "big.fits", b"x" * 500)
    assert r.status_code == 413


def test_unknown_or_expired_image_is_404(client):
    msg = "Image expired — load it again"
    r = client.post("/api/astrometry/detect", json={"image_id": "nope"})
    assert r.status_code == 404 and r.json()["detail"] == msg
    r = client.post("/api/astrometry/snap",
                    json={"image_id": "nope", "x": 1, "y": 1, "role": "star"})
    assert r.status_code == 404 and r.json()["detail"] == msg
    # a real session that has aged out
    clk = Clock()
    client.app_state.astrometry._clock = clk
    data, _, _ = _object_frame(n_stars=4, seed=1)
    iid = _upload(client, "f.fits", _fits_bytes(data)).json()["image_id"]
    clk.t += 31 * 60
    r = client.post("/api/astrometry/detect", json={"image_id": iid})
    assert r.status_code == 404 and r.json()["detail"] == msg


def test_snap_validates_role(client):
    r = client.post("/api/astrometry/snap",
                    json={"image_id": "x", "x": 1, "y": 1, "role": "moon"})
    assert r.status_code == 422


def test_detect_errors_are_readable(client, monkeypatch):
    data, _, _ = _object_frame(n_stars=4, seed=1)
    iid = _upload(client, "f.fits", _fits_bytes(data)).json()["image_id"]

    def boom(_s):
        raise RuntimeError("secret internal path C:/x")

    monkeypatch.setattr(service, "detect", boom)
    r = client.post("/api/astrometry/detect", json={"image_id": iid})
    assert r.status_code == 503 and "secret" not in r.text
    assert r.json()["detail"] == "Streak detection failed on this image"


# -- real frames (read in place from ssa-toolkit, never copied) --------------
OW = [SSA_DATA / f"OW0021_{t}.tif" for t in ("20250404130130", "20250404130330", "20250404130530")]


@pytest.mark.parametrize("path", OW, ids=lambda p: p.name)
def test_real_ow0021_frame(path):
    if not path.is_file():
        pytest.skip("ssa-toolkit sample frame not present")
    from analysis.astrometry.io import load_image

    s = ImageSessions()
    img = load_image(str(path))
    sess = s.get(s.add(img, service.build_meta(img)))
    out = service.detect(sess)
    assert out["n_streaks"] >= 10
    assert out["target"] is not None and out["target"]["shape"] == "point"
    # ssa-toolkit's own engine gave these target positions (scratch comparison, T5.2a)
    expect = {"130130": (5152.17, 3151.75), "130330": (5029.30, 3489.48),
              "130530": (4878.99, 3455.37)}[path.stem[-6:]]
    assert np.hypot(out["target"]["x"] - expect[0], out["target"]["y"] - expect[1]) < 1.0
    # a snap near the target stays on it
    sn = service.snap(sess, expect[0] + 2, expect[1] + 2, "target")
    assert np.hypot(sn["x"] - expect[0], sn["y"] - expect[1]) < 2.0
