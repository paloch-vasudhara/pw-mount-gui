"""T5.2g: what nova is sent (x,y list default, downscaled image fallback), the header sky hint,
safer uploads (retry rules, deadline, cancel), log file + stage texts. Fake HTTP / fake clients
only; the real nova.astrometry.net is never contacted."""

from __future__ import annotations

import json
import logging
import os
import time

import numpy as np
import pytest
import requests
from astropy.io import fits
from astropy.wcs import WCS
from fastapi.testclient import TestClient

from analysis import logsetup
from analysis.app import create_app
from analysis.astrometry import api as astro_api
from analysis.astrometry import nova, service
from analysis.astrometry import solve as solve_mod
from analysis.astrometry.io import ImageData, load_image
from analysis.astrometry.jobs import JobCancelled
from analysis.tests.test_astrometry_resilience import (
    SESSION,
    FakeHttp,
    client,
    ok,
)
from analysis.tests.test_astrometry_run import KEY, H, W, _body, _wait


def tan_wcs(width, height, scale_arcsec, ra=150.0, dec=20.0) -> WCS:
    d = scale_arcsec / 3600.0
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [width / 2.0 + 0.5, height / 2.0 + 0.5]
    w.wcs.cd = [[-d, 0.0], [0.0, d]]
    return w


def hdr_dict(w: WCS) -> dict:
    return {k: (v if isinstance(v, (str, int, float)) else float(v))
            for k, v in dict(w.to_header()).items() if k not in ("COMMENT", "HISTORY")}


class CapturingHttp(FakeHttp):
    """FakeHttp that records the upload's request-json params and file name."""

    def __init__(self, **over):
        super().__init__(**over)
        self.upload_params: dict = {}
        self.upload_name = None

    def post(self, url, **kw):
        if not url.endswith("/login"):
            self.upload_params = json.loads(kw["data"]["request-json"])
            self.upload_name = kw["files"]["file"][0]
        return super().post(url, **kw)


# -- A: the x,y list ---------------------------------------------------------------------
def test_xylist_table_columns_one_based_brightest_first(tmp_path):
    p = str(tmp_path / "f.xyls")
    xy = [(10.0, 20.0), (100.5, 50.25), (30.0, 40.0)]
    n = solve_mod.write_xylist(xy, 400, 300, p, flux=[2, 5, 3])
    assert n == 3
    with fits.open(p) as h:
        t = h[1].data
        assert h[1].columns.names == ["X", "Y", "FLUX"]
        assert all(str(h[1].columns[c].format) == "E" for c in range(3))   # float32
        assert h[1].header["IMAGEW"] == 400 and h[1].header["IMAGEH"] == 300
        assert list(t["X"]) == [101.5, 31.0, 11.0]     # 0-based + 1, brightest first
        assert list(t["Y"]) == [51.25, 41.0, 21.0]
        assert list(t["FLUX"]) == [5, 3, 2]


def test_xylist_without_flux_keeps_order_and_has_no_flux_column(tmp_path):
    p = str(tmp_path / "f.xyls")
    solve_mod.write_xylist([(1, 2), (3, 4)], 10, 10, p)
    with fits.open(p) as h:
        assert h[1].columns.names == ["X", "Y"] and list(h[1].data["X"]) == [2.0, 4.0]


class RoundTripClient:
    """Plays nova for a known truth: checks the file it is given against the truth, then
    answers with the truth's WCS (``scale``: the image-mode downscale to answer in)."""

    def __init__(self, truth: WCS, world, f: float = 1.0):
        self.truth, self.world, self.f = truth, world, f
        self.seen: dict = {}

    def solve(self, image_path, *, width, height, scale_est_arcsec, timeout, progress,
              check_cancel, resume_subid=None, hint=None):
        self.seen = {"path": image_path, "w": width, "h": height, "scale": scale_est_arcsec,
                     "hint": hint, "size": os.path.getsize(image_path)}
        if image_path.endswith(".xyls"):
            with fits.open(image_path) as h:
                t = h[1].data
                got = np.column_stack([t["X"], t["Y"]])
            # the 1-based FITS pixels a true solver would see for those sky positions
            want = self.truth.all_world2pix(self.world, 1)
            order = np.argsort(np.argsort(got[:, 0]))   # compare as sets
            assert np.allclose(np.sort(got, axis=0), np.sort(want, axis=0), atol=1e-3), order
            hdr = hdr_dict(self.truth)
        else:
            with fits.open(image_path) as h:
                self.seen["img"] = h[0].data
                self.seen["bitpix"] = h[0].header["BITPIX"]
            f = self.f
            ds = tan_wcs(width, height, 1.0)
            ds.wcs.crval = self.truth.wcs.crval
            ds.wcs.cd = np.array(self.truth.wcs.cd) * f
            ds.wcs.crpix = [(self.truth.wcs.crpix[0] - 0.5) / f + 0.5,
                            (self.truth.wcs.crpix[1] - 0.5) / f + 0.5]
            hdr = hdr_dict(ds)
            hdr["A_ORDER"] = 2          # SIP terms must be dropped, not mis-scaled
            hdr["A_2_0"] = 1e-6
            hdr["CTYPE1"] = "RA---TAN-SIP"
        return nova.NovaSolution(150.0, 20.0, 0.0, 1.25 * (self.f if "img" in self.seen else 1),
                                 -1, hdr)


def _truth(width, height):
    truth = tan_wcs(width, height, 1.25)
    rng = np.random.default_rng(3)
    xy0 = np.column_stack([rng.uniform(0, width - 1, 40), rng.uniform(0, height - 1, 40)])
    return truth, xy0, truth.all_pix2world(xy0, 0)


def _arcsec_err(a, b):
    ra1, de1, ra2, de2 = map(np.radians, (a[0], a[1], b[0], b[1]))
    c = np.sin(de1) * np.sin(de2) + np.cos(de1) * np.cos(de2) * np.cos(ra1 - ra2)
    return float(np.degrees(np.arccos(min(1.0, c))) * 3600.0)


def test_xylist_wcs_roundtrip_maps_zero_based_pixels_exactly():
    truth, xy0, world = _truth(1200, 800)
    c = RoundTripClient(truth, world)
    res = solve_mod.solve_points(c, [tuple(p) for p in xy0], 1200, 800, 1.25, 30.0,
                                 mode="xylist")
    assert c.seen["path"].endswith(".xyls") and (c.seen["w"], c.seen["h"]) == (1200, 800)
    assert c.seen["size"] < 20_000
    wcs = solve_mod.build_wcs(res.wcs_header)
    for x, y in [(0, 0), (1199, 799), (600, 400), (123.4, 567.8)]:
        got = solve_mod.map_point(wcs, x, y)
        want = truth.all_pix2world([[x, y]], 0)[0]
        assert _arcsec_err(got, (want[0] % 360, want[1])) < 0.01
    # a catalogue star: its 0-based pixel maps back to its RA / Dec
    ra, dec = solve_mod.map_point(wcs, *xy0[5])
    assert _arcsec_err((ra, dec), world[5]) < 0.01


def test_image_mode_downscaled_roundtrip_corners_within_half_arcsec():
    width, height = 6000, 4000
    truth, xy0, world = _truth(width, height)
    f = solve_mod.downscale_factor(width, height)
    assert f == pytest.approx(6000 / 2048)
    c = RoundTripClient(truth, world, f=f)
    res = solve_mod.solve_points(c, [tuple(p) for p in xy0], width, height, 1.25, 30.0,
                                 mode="image")
    assert c.seen["path"].endswith(".fits")
    assert (c.seen["w"], c.seen["h"]) == (2048, 1366)                  # ceil(W/f), ceil(H/f)
    assert c.seen["scale"] == pytest.approx(1.25 * f)
    assert c.seen["bitpix"] == 8 and c.seen["img"].dtype == np.uint8
    assert c.seen["size"] < 4_000_000
    # a star's blob sits at ((x0+.5)/f - .5, ...) in the downscaled array
    x0, y0 = xy0[0]
    xd, yd = (x0 + 0.5) / f - 0.5, (y0 + 0.5) / f - 0.5
    win = c.seen["img"][int(round(yd)) - 3:int(round(yd)) + 4,
                        int(round(xd)) - 3:int(round(xd)) + 4]
    assert win.max() > 150
    assert res.scale_arcsec_px == pytest.approx(1.25)
    assert not any(k.startswith("A_") for k in res.wcs_header)           # SIP dropped
    assert res.wcs_header["CTYPE1"] == "RA---TAN"
    wcs = solve_mod.build_wcs(res.wcs_header)
    for x, y in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1),
                 (3000, 2000)]:
        got = solve_mod.map_point(wcs, x, y)
        want = truth.all_pix2world([[x, y]], 0)[0]
        assert _arcsec_err(got, (want[0] % 360, want[1])) <= 0.5


def test_rescale_wcs_header_cdelt_form_and_identity():
    h = {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRPIX1": 11.0, "CRPIX2": 6.0,
         "CDELT1": -0.001, "CDELT2": 0.001}
    out = solve_mod.rescale_wcs_header(h, 2.0)
    assert out["CDELT1"] == pytest.approx(-0.0005) and out["CRPIX1"] == pytest.approx(21.5)
    assert out["CRPIX2"] == pytest.approx(11.5)
    assert solve_mod.rescale_wcs_header(h, 1.0) == h


def test_hint_radius_clamped():
    assert solve_mod.hint_radius_deg(None, 9576, 6388) == 10.0
    # 1.25"/px, 9576x6388: diagonal 4 deg -> 1.5 x 2 = 3 deg
    assert solve_mod.hint_radius_deg(1.25, 9576, 6388) == pytest.approx(3.0, abs=0.01)
    assert solve_mod.hint_radius_deg(1.25, 100, 100) == 1.0
    assert solve_mod.hint_radius_deg(60.0, 9576, 6388) == 10.0


# -- the env switch ----------------------------------------------------------------------
def test_env_switch(monkeypatch):
    monkeypatch.delenv(solve_mod.UPLOAD_ENV, raising=False)
    assert solve_mod.upload_mode() == "xylist"
    monkeypatch.setenv(solve_mod.UPLOAD_ENV, "image")
    assert solve_mod.upload_mode() == "image"
    monkeypatch.setenv(solve_mod.UPLOAD_ENV, "IMAGE ")
    assert solve_mod.upload_mode() == "image"
    monkeypatch.setenv(solve_mod.UPLOAD_ENV, "bogus")
    assert solve_mod.upload_mode() == "xylist"


class SeeingNova:
    def __init__(self):
        self.calls = []

    def solve(self, image_path, *, width, height, scale_est_arcsec, timeout=300, progress=None,
              check_cancel=None, resume_subid=None, hint=None):
        self.calls.append({"path": image_path, "w": width, "h": height,
                           "scale": scale_est_arcsec, "hint": hint})
        w = tan_wcs(width, height, scale_est_arcsec or 5.0)
        return nova.NovaSolution(150.0, 20.0, 0.0, scale_est_arcsec or 5.0, -1, hdr_dict(w))


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv(solve_mod.UPLOAD_ENV, raising=False)
    app = create_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    fake = SeeingNova()
    app.state.nova_factory = lambda key: fake

    def add(header=None):
        img = ImageData(data=np.zeros((H, W), np.float32), width=W, height=H,
                        source_format="fits", header=header or {})
        return app.state.astrometry.add(img, {"dims": [W, H]})

    with TestClient(app) as c:
        yield c, app, fake, add


def test_run_default_uploads_an_xylist_and_no_hint_without_a_header(env):
    c, _app, fake, add = env
    r = c.post("/api/astrometry/run", json=_body(add()))
    assert r.status_code == 202
    assert _wait(c, r.json()["job_id"])["state"] == "done"
    call = fake.calls[0]
    assert call["path"].endswith(".xyls") and (call["w"], call["h"]) == (W, H)
    assert call["hint"] is None


def test_run_in_image_mode_sends_the_scaled_image(env, monkeypatch):
    c, _app, fake, add = env
    monkeypatch.setenv(solve_mod.UPLOAD_ENV, "image")
    r = c.post("/api/astrometry/run", json=_body(add()))
    assert _wait(c, r.json()["job_id"])["state"] == "done"
    call = fake.calls[0]
    assert call["path"].endswith(".fits") and (call["w"], call["h"]) == (W, H)   # f = 1


def test_run_passes_the_header_hint_and_the_job_spec_keeps_it(env):
    c, app, fake, add = env
    iid = add({"RA": 150.5, "DEC": 20.25})
    r = c.post("/api/astrometry/run", json=_body(iid))
    j = _wait(c, r.json()["job_id"])
    assert j["state"] == "done"
    ra, dec, rad = fake.calls[0]["hint"]
    assert (ra, dec) == (150.5, 20.25) and 1.0 <= rad <= 10.0
    assert rad == pytest.approx(solve_mod.hint_radius_deg(1.2510, W, H), abs=0.01)


def test_fingerprint_depends_on_hint_and_mode():
    s = [(1.0, 2.0), (3.0, 4.0)]
    base = astro_api._fingerprint("i", s, 1.25)
    assert astro_api._fingerprint("i", s, 1.25) == base
    assert astro_api._fingerprint("i", s, 1.25, (150.0, 20.0, 3.0), "xylist") != base
    assert (astro_api._fingerprint("i", s, 1.25, (150.0, 20.0, 3.0), "xylist")
            != astro_api._fingerprint("i", s, 1.25, (150.0, 20.0, 3.0), "image"))
    assert (astro_api._fingerprint("i", s, 1.25, (150.0, 21.0, 3.0), "xylist")
            != astro_api._fingerprint("i", s, 1.25, (150.0, 20.0, 3.0), "xylist"))


# -- B: header pointing ------------------------------------------------------------------
def test_header_pointing_degrees():
    assert service.header_pointing({"RA": 10.5, "DEC": -20.25}) == (10.5, -20.25)
    assert service.header_pointing({"RA": "123.4", "DEC": "45"}) == (123.4, 45.0)


def test_header_pointing_sexagesimal():
    ra, dec = service.header_pointing({"OBJCTRA": "12 30 00", "OBJCTDEC": "-05 30 00"})
    assert ra == pytest.approx(187.5) and dec == pytest.approx(-5.5)
    ra, dec = service.header_pointing({"OBJCTRA": "01:00:00.0", "OBJCTDEC": "+89:59:59"})
    assert ra == pytest.approx(15.0) and dec == pytest.approx(89 + 59 / 60 + 59 / 3600)


def test_header_pointing_crval_needs_a_wcs():
    wcs = {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRPIX1": 5, "CRPIX2": 6,
           "CRVAL1": 83.8, "CRVAL2": -5.4}
    assert service.header_pointing(wcs) == (83.8, -5.4)
    assert service.header_pointing({"CRVAL1": 83.8, "CRVAL2": -5.4}) is None
    assert service.header_pointing({**wcs, "CTYPE1": "GLON-TAN"}) is None


def test_header_pointing_none_and_invalid():
    assert service.header_pointing(None) is None
    assert service.header_pointing({}) is None
    assert service.header_pointing({"EXPTIME": 5}) is None
    assert service.header_pointing({"RA": 400.0, "DEC": 10.0}) is None
    assert service.header_pointing({"RA": 10.0, "DEC": 95.0}) is None
    assert service.header_pointing({"RA": 0.0, "DEC": 0.0}) is None
    assert service.header_pointing({"RA": "abc", "DEC": "def"}) is None


def test_fits_loader_keeps_the_pointing_keys(tmp_path):
    p = tmp_path / "a.fits"
    hdu = fits.PrimaryHDU(np.zeros((20, 30), np.float32))
    hdu.header["OBJCTRA"], hdu.header["OBJCTDEC"] = "05 35 17", "-05 23 28"
    hdu.writeto(p)
    pt = service.header_pointing(load_image(str(p)).header)
    assert pt == pytest.approx((83.8208, -5.3911), abs=1e-3)


# -- C: safer uploads --------------------------------------------------------------------
@pytest.fixture()
def xyls_path(tmp_path):
    p = tmp_path / "f.xyls"
    p.write_bytes(b"x")
    return str(p)


def solve(c, path, **kw):
    return c.solve(path, width=W, height=H, scale_est_arcsec=1.25, **kw)


def test_upload_params_xylist_and_hint(xyls_path):
    f = CapturingHttp()
    c, _ = client(f)
    solve(c, xyls_path, hint=(150.0, 20.0, 3.0))
    p = f.upload_params
    assert "downsample_factor" not in p
    assert p["image_width"] == W and p["image_height"] == H
    assert (p["center_ra"], p["center_dec"], p["radius"]) == (150.0, 20.0, 3.0)
    assert p["scale_units"] == "arcsecperpix" and p["scale_est"] == 1.25
    assert p["scale_err"] == 25.0 and p["publicly_visible"] == "n"
    assert p["allow_commercial_use"] == "n" and p["allow_modifications"] == "n"
    assert f.upload_name == "field.xyls"


def test_upload_params_without_hint_and_image_mode(tmp_path):
    img = tmp_path / "f.fits"
    img.write_bytes(b"x")
    f = CapturingHttp()
    c, _ = client(f)
    solve(c, str(img))
    p = f.upload_params
    assert "center_ra" not in p and "radius" not in p
    assert "image_width" not in p and "downsample_factor" not in p
    assert f.upload_name == "field.fits"


def test_connect_failures_are_retried_on_upload(xyls_path):
    for exc in (requests.ConnectTimeout(), requests.ConnectionError()):
        f = FakeHttp(upload=[exc, ok({"status": "success", "subid": 7})])
        c, clk = client(f)
        solve(c, xyls_path)
        assert f.calls.count("upload") == 2 and clk.waits == [2.0]


def test_read_timeout_after_send_is_not_retried(xyls_path):
    f = FakeHttp(upload=[requests.ReadTimeout(), ok({"status": "success", "subid": 7})])
    c, _ = client(f)
    with pytest.raises(nova.NovaError) as ei:
        solve(c, xyls_path)
    assert ei.value.kind == nova.UNREACHABLE and ei.value.reason == nova.UPLOAD_SENT
    assert f.calls.count("upload") == 1
    assert astro_api._nova_error_text(ei.value).startswith("Upload sent but astrometry.net")
    assert "My submissions" in astro_api._nova_error_text(ei.value)


def test_connection_drop_after_send_is_not_retried(xyls_path):
    class Aborted(Exception):
        pass

    err = requests.ConnectionError(Aborted("Connection aborted."))
    f = FakeHttp(upload=[err, ok({"status": "success", "subid": 7})])
    c, _ = client(f)
    with pytest.raises(nova.NovaError) as ei:
        solve(c, xyls_path)
    assert ei.value.reason == nova.UPLOAD_SENT and f.calls.count("upload") == 1


def test_login_read_timeout_is_still_retried(xyls_path):
    f = FakeHttp(login=[requests.ReadTimeout(), ok({"status": "success", "session": SESSION})])
    c, _ = client(f)
    solve(c, xyls_path)
    assert f.calls.count("login") == 2


def test_upload_deadline_ends_the_stage(xyls_path):
    f = FakeHttp(login=[requests.ConnectTimeout()])
    c, clk = client(f, upload_deadlines=(6.0, 300.0))
    with pytest.raises(nova.NovaError) as ei:
        solve(c, xyls_path)
    assert ei.value.kind == nova.UNREACHABLE and ei.value.reason == nova.UPLOAD_TIMEOUT
    assert astro_api._nova_error_text(ei.value) == "astrometry.net upload timed out"
    assert clk.waits == [2.0, 5.0]          # attempt 3 starts at t=7 > 6: no further request
    assert f.calls.count("login") == 2


def test_deadline_defaults_xylist_120_image_300():
    assert (nova.UPLOAD_DEADLINE_XYLS_S, nova.UPLOAD_DEADLINE_IMAGE_S) == (120.0, 300.0)
    c, _ = client(FakeHttp())
    assert c._deadlines == (120.0, 300.0)


def test_cancel_is_checked_before_each_request_and_between_retries(xyls_path):
    f = FakeHttp()
    c, _ = client(f)

    def stop():
        raise JobCancelled()

    with pytest.raises(JobCancelled):
        solve(c, xyls_path, check_cancel=stop)
    assert f.calls == []                     # cancelled before the login went out
    n = {"i": 0}

    def stop_on_third():
        n["i"] += 1
        if n["i"] >= 3:
            raise JobCancelled()

    f = FakeHttp(upload=[requests.ConnectTimeout()])
    c, _ = client(f)
    with pytest.raises(JobCancelled):
        solve(c, xyls_path, check_cancel=stop_on_third)
    assert f.calls.count("upload") == 1       # cancelled before the retry


# -- D: stage texts, log file ------------------------------------------------------------
def test_stage_texts_carry_the_attempt(xyls_path):
    f = FakeHttp(upload=[requests.ConnectTimeout(), ok({"status": "success", "subid": 7})])
    c, _ = client(f)
    seen = []
    solve(c, xyls_path, progress=lambda **kw: seen.append(kw))
    texts = [k["text"] for k in seen if k.get("text")]
    assert texts == ["Logging in to astrometry.net · attempt 1/4",
                     "Uploading to astrometry.net · attempt 1/4",
                     "Uploading to astrometry.net · attempt 2/4"]


def test_job_stage_text_shows_the_attempt():
    from analysis.astrometry.jobs import JobRegistry
    holder: dict = {}
    texts: list[str] = []

    class Spy(FakeHttp):
        def post(self, url, **kw):
            if holder.get("job") is not None:
                texts.append(holder["job"].stage_text)
            return super().post(url, **kw)

    f = Spy(upload=[requests.ConnectTimeout(), ok({"status": "success", "subid": 7})])
    c = nova.NovaHttpClient(KEY, http=f, sleep=lambda s, cc=None: None, poll_s=0.0)
    reg = JobRegistry()
    spec = {"width": W, "height": H, "epoch": "2025-04-04T13:01:30.000Z", "exposure_s": 10.0,
            "target": (250.0, 100.0), "stars": [(30.0 + 25 * i, 40.0 + 15 * i) for i in range(12)],
            "hint_scale": 1.25, "flux": [2.0] * 12, "sky_hint": None, "upload_mode": "xylist"}

    def init(job):
        holder["job"] = job

    job = reg.create_and_run(astro_api._make_worker(c, spec=spec), init)
    t0 = time.time()
    while job.active and time.time() - t0 < 5:
        time.sleep(0.01)
    assert job.state == "done"
    assert texts == ["Logging in to astrometry.net · attempt 1/4",
                     "Uploading to astrometry.net · attempt 1/4",
                     "Uploading to astrometry.net · attempt 2/4"]


def test_log_file_has_the_lines_and_never_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv(solve_mod.UPLOAD_ENV, raising=False)
    path = logsetup.configure_logging(tmp_path)
    try:
        assert path == tmp_path / "analysis.log"
        app = create_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        fake = FakeHttp(upload=[requests.ConnectTimeout(),
                                ok({"status": "success", "subid": 7})])
        from analysis.tests.test_astrometry_resilience import Clock
        clk = Clock()
        app.state.nova_factory = lambda key: nova.NovaHttpClient(
            key, http=fake, poll_s=5.0, sleep=clk.sleep, clock=clk.now)
        img = ImageData(data=np.zeros((H, W), np.float32), width=W, height=H,
                        source_format="fits", header={"RA": 150.0, "DEC": 20.0})
        iid = app.state.astrometry.add(img, {"dims": [W, H]})
        with TestClient(app) as c:
            r = c.post("/api/astrometry/run", json=_body(iid))
            assert _wait(c, r.json()["job_id"])["state"] == "done"
        for h in logging.getLogger().handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        assert KEY not in text and SESSION not in text and "nova.astrometry.net" not in text
        assert "nova step=upload attempt=1/4 result=ConnectTimeout" in text
        assert "nova step=upload attempt=2/4 result=200" in text
        assert "astrometry upload mode=xylist payload_bytes=" in text
        assert "stars=12 hint=yes" in text
    finally:
        for h in [h for h in logging.getLogger().handlers if getattr(h, "_pw_analysis", False)]:
            logging.getLogger().removeHandler(h)
            h.close()
        logging.getLogger("analysis").setLevel(logging.NOTSET)
        logging.getLogger().setLevel(logging.WARNING)


def test_log_dir_env_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PW_LOG_DIR", str(tmp_path / "x"))
    assert logsetup.log_dir() == tmp_path / "x"
    monkeypatch.delenv("PW_LOG_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    assert logsetup.log_dir() == tmp_path / "la" / "pw-mount-control" / "logs"
    assert (logsetup.MAX_BYTES, logsetup.BACKUPS) == (2 * 1024 * 1024, 4)
