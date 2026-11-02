"""astrometry.net resilience: retries, poll tolerance, results-not-ready, resume, re-attach,
fingerprint reuse, log hygiene. A scripted fake HTTP layer; no network, all waits instant."""

from __future__ import annotations

import io
import logging
import time

import numpy as np
import pytest
import requests
from astropy.io import fits
from fastapi.testclient import TestClient

from analysis.app import create_app
from analysis.astrometry import nova
from analysis.astrometry.io import ImageData

from .test_astrometry_run import KEY, H, W, _body, _dets, _wait

SESSION = "SESSION-TOKEN-XYZ-98765"
CAL = {"ra": 150.0, "dec": 20.0, "orientation": 0.0, "pixscale": 1.251, "parity": -1.0}


def wcs_bytes(scale: float = 1.251) -> bytes:
    d = scale / 3600.0
    h = fits.PrimaryHDU().header
    for k, v in {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": 150.0, "CRVAL2": 20.0,
                 "CRPIX1": W / 2 + 0.5, "CRPIX2": H / 2 + 0.5, "CD1_1": -d, "CD1_2": 0.0,
                 "CD2_1": 0.0, "CD2_2": d}.items():
        h[k] = v
    buf = io.BytesIO()
    fits.PrimaryHDU(header=h).writeto(buf)
    return buf.getvalue()


class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def ok(data):
    return Resp(200, data)


class FakeHttp:
    """Scripted session. ``script[route]`` is a list consumed in order (the last entry
    repeats); an entry is a Resp or an exception instance. Every call is recorded."""

    def __init__(self, **over):
        self.script = {
            "login": [ok({"status": "success", "session": SESSION})],
            "upload": [ok({"status": "success", "subid": 7})],
            "submission": [ok({"jobs": [9]})],
            "job_status": [ok({"status": "success"})],
            "calibration": [ok(CAL)],
            "wcs_file": [Resp(200, None, wcs_bytes())],
        }
        self.script.update(over)
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _go(self, route):
        self.calls.append(route)
        seq = self.script[route]
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url, **kw):
        return self._go("login" if url.endswith("/login") else "upload")

    def get(self, url, **kw):
        if "/submissions/" in url:
            return self._go("submission")
        if url.endswith("/calibration"):
            return self._go("calibration")
        if "/wcs_file/" in url:
            return self._go("wcs_file")
        return self._go("job_status")


class Clock:
    """Fake time: every wait advances it, so a 30 min limit passes instantly."""

    def __init__(self):
        self.t = 0.0
        self.waits: list[float] = []

    def now(self):
        return self.t

    def sleep(self, seconds, check_cancel=None):
        if check_cancel:
            check_cancel()
        self.waits.append(seconds)
        self.t += seconds


def client(fake, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("poll_s", 5.0)
    c = nova.NovaHttpClient(KEY, http=fake, sleep=clock.sleep, clock=clock.now, **kw)
    return c, clock


@pytest.fixture()
def fits_path(tmp_path):
    p = tmp_path / "f.fits"
    p.write_bytes(b"x")
    return str(p)


def solve(c, path, **kw):
    return c.solve(path, width=W, height=H, scale_est_arcsec=1.25, **kw)


# -- per-request retry ---------------------------------------------------------------
def test_409_on_calibration_then_success(fits_path):
    f = FakeHttp(calibration=[Resp(409), ok(CAL)])
    c, clk = client(f)
    assert solve(c, fits_path).pixscale_arcsec == pytest.approx(1.251)
    assert f.calls.count("calibration") == 2 and clk.waits == [2.0]


def test_503_burst_on_a_poll_then_success(fits_path):
    f = FakeHttp(submission=[Resp(503), Resp(503), ok({"jobs": [9]})])
    c, clk = client(f)
    solve(c, fits_path)
    assert clk.waits == [2.0, 5.0]


def test_timeout_mid_poll_then_success(fits_path):
    f = FakeHttp(job_status=[requests.Timeout(), ok({"status": "success"})])
    c, clk = client(f)
    solve(c, fits_path)
    assert f.calls.count("job_status") == 2 and clk.waits == [2.0]


def test_login_retries_and_gives_up_after_three_retries(fits_path):
    f = FakeHttp(login=[requests.ConnectionError()])
    c, clk = client(f)
    with pytest.raises(nova.NovaError) as ei:
        solve(c, fits_path)
    assert ei.value.kind == nova.UNREACHABLE
    assert f.calls.count("login") == 4 and clk.waits == [2.0, 5.0, 10.0]
    assert "upload" not in f.calls


def test_invalid_key_is_not_retried(fits_path):
    f = FakeHttp(login=[Resp(403)])
    c, clk = client(f)
    with pytest.raises(nova.NovaError) as ei:
        solve(c, fits_path)
    assert ei.value.kind == nova.INVALID_KEY and f.calls == ["login"]


def test_five_consecutive_failed_polls_is_busy(fits_path):
    f = FakeHttp(submission=[Resp(503)])
    c, clk = client(f)
    with pytest.raises(nova.NovaError) as ei:
        solve(c, fits_path)
    e = ei.value
    assert (e.kind, e.status, e.retries) == (nova.BUSY, 503, 3)
    assert f.calls.count("submission") == 5 * 4


def test_a_failed_poll_does_not_end_the_job(fits_path):
    # 4 failed polls (each exhausts its retries), the 5th answers
    f = FakeHttp(submission=[Resp(503)] * 16 + [ok({"jobs": [9]})])
    c, _ = client(f)
    assert solve(c, fits_path).ra_deg == 150.0


def test_failed_polls_must_be_consecutive(fits_path):
    seq = []
    for _ in range(4):                      # 4 bad polls, a good "queued", repeated
        seq += [Resp(503)] * 4 + [ok({"jobs": []})]
    seq += [ok({"jobs": [9]})]
    c, _ = client(FakeHttp(submission=seq))
    assert solve(c, fits_path).ra_deg == 150.0


# -- upload never double-submits -------------------------------------------------------
def test_upload_500_is_not_retried(fits_path):
    f = FakeHttp(upload=[Resp(500), ok({"status": "success", "subid": 7})])
    c, clk = client(f)
    with pytest.raises(nova.NovaError) as ei:
        solve(c, fits_path)
    assert ei.value.kind == nova.BUSY and ei.value.status == 500 and ei.value.retries == 0
    assert f.calls.count("upload") == 1 and clk.waits == []


def test_upload_409_is_not_retried(fits_path):
    f = FakeHttp(upload=[Resp(409), ok({"status": "success", "subid": 7})])
    c, _ = client(f)
    with pytest.raises(nova.NovaError):
        solve(c, fits_path)
    assert f.calls.count("upload") == 1


def test_upload_connection_error_before_a_response_is_retried(fits_path):
    f = FakeHttp(upload=[requests.ConnectionError(), requests.Timeout(),
                         ok({"status": "success", "subid": 7})])
    c, clk = client(f)
    solve(c, fits_path)
    assert f.calls.count("upload") == 3 and clk.waits == [2.0, 5.0]


def test_upload_503_and_429_are_retried(fits_path):
    f = FakeHttp(upload=[Resp(429), Resp(503), ok({"status": "success", "subid": 7})])
    c, _ = client(f)
    solve(c, fits_path)
    assert f.calls.count("upload") == 3


# -- results not ready -------------------------------------------------------------------
def test_results_not_ready_then_ready(fits_path):
    f = FakeHttp(wcs_file=[Resp(200, None, b""), Resp(404), Resp(200, None, wcs_bytes())])
    c, clk = client(f)
    solve(c, fits_path)
    assert clk.waits == [3.0, 3.0]


def test_results_never_ready_fails_after_30s(fits_path):
    f = FakeHttp(wcs_file=[Resp(200, None, b"")])
    c, clk = client(f)
    with pytest.raises(nova.NovaError):
        solve(c, fits_path)
    assert sum(clk.waits) == pytest.approx(30.0)


def test_calibration_empty_is_not_ready(fits_path):
    f = FakeHttp(calibration=[ok({}), ok(CAL)])
    c, _ = client(f)
    assert solve(c, fits_path).ra_deg == 150.0


# -- slow is not failed -------------------------------------------------------------------
def test_slow_submission_under_30_minutes_is_done(fits_path):
    f = FakeHttp(submission=[ok({"jobs": []})] * 300 + [ok({"jobs": [9]})])   # 1500 s queued
    c, clk = client(f)
    assert solve(c, fits_path, timeout=nova.SOLVE_TIMEOUT_S).ra_deg == 150.0
    assert clk.t >= 1500


def test_solve_limit_is_30_minutes():
    assert nova.SOLVE_TIMEOUT_S == 1800.0 and nova.REQUEST_TIMEOUT_S == 60.0


def test_nova_job_failure_status_is_unsolvable(fits_path):
    f = FakeHttp(job_status=[ok({"status": "failure"})])
    c, _ = client(f)
    with pytest.raises(nova.NovaError) as ei:
        solve(c, fits_path)
    assert ei.value.unsolvable


def test_resume_subid_logs_in_but_never_uploads(fits_path):
    f = FakeHttp()
    c, _ = client(f)
    solve(c, fits_path, resume_subid=42)
    assert f.calls[0] == "login" and "upload" not in f.calls


# -- the API: jobs, resume, re-attach, fingerprint ----------------------------------------
class Env:
    def __init__(self, c, app, fake, clk, iid):
        self.c, self.app, self.fake, self.clk, self.iid = c, app, fake, clk, iid

    def run(self, **over):
        r = self.c.post("/api/astrometry/run", json=_body(self.iid, **over))
        assert r.status_code == 202, r.text
        return r.json()["job_id"]

    def finish(self, jid):
        return _wait(self.c, jid)


@pytest.fixture()
def env():
    app = create_app(lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    fake = FakeHttp()
    clk = Clock()

    def real_sleep(seconds, check_cancel=None):
        time.sleep(0.002)
        clk.sleep(seconds, check_cancel)

    app.state.nova_factory = lambda key: nova.NovaHttpClient(
        key, http=fake, poll_s=5.0, sleep=real_sleep, clock=clk.now)
    img = ImageData(data=np.zeros((H, W), np.float32), width=W, height=H, source_format="fits")
    iid = app.state.astrometry.add(img, {"dims": [W, H]})
    with TestClient(app) as c:
        yield Env(c, app, fake, clk, iid)


def test_busy_failure_text_and_resumable_after_upload(env):
    env.fake.script["submission"] = [Resp(503)]
    j = env.finish(env.run())
    assert j["state"] == "failed" and j["error"] == "astrometry.net busy (HTTP 503) — retried 3×"
    assert j["resumable"] is True


def test_unreachable_text(env):
    env.fake.script["login"] = [requests.ConnectionError()]
    j = env.finish(env.run())
    assert j["error"] == "astrometry.net unreachable (network)" and j["resumable"] is False


def test_upload_500_fails_not_resumable_and_not_retried(env):
    env.fake.script["upload"] = [Resp(500)]
    j = env.finish(env.run())
    assert j["state"] == "failed" and j["resumable"] is False
    assert env.fake.calls.count("upload") == 1


def test_timeout_keeps_submission_and_stops_polling(env):
    env.fake.script["submission"] = [ok({"jobs": []})]
    j = env.finish(env.run())
    assert j["state"] == "timeout" and j["error"] == "Solve timed out" and j["resumable"]
    n = len(env.fake.calls)
    time.sleep(0.3)
    assert len(env.fake.calls) == n          # no stray thread keeps polling


def test_unsolvable_is_not_resumable_and_rerun_uploads_again(env):
    env.fake.script["job_status"] = [ok({"status": "failure"})]
    j = env.finish(env.run())
    assert j["state"] == "failed" and j["error"] == "astrometry.net could not solve this frame"
    assert j["resumable"] is False
    env.fake.script["job_status"] = [ok({"status": "success"})]
    assert env.finish(env.run())["state"] == "done"
    assert env.fake.calls.count("upload") == 2


def test_rerun_after_timeout_reattaches_without_upload(env):
    env.fake.script["submission"] = [ok({"jobs": []})]
    assert env.finish(env.run())["state"] == "timeout"
    env.fake.script["submission"] = [ok({"jobs": [9]})]
    jid = env.run()
    assert env.finish(jid)["state"] == "done"
    assert env.fake.calls.count("upload") == 1
    assert env.app.state.astro_jobs.get(jid).nova_subid == 7


def test_reattach_stage_text(env):
    env.fake.script["submission"] = [ok({"jobs": []})]
    env.finish(env.run())
    gate = [ok({"jobs": []})]
    env.fake.script["submission"] = gate
    jid = env.run()
    texts = set()
    for _ in range(200):
        texts.add(env.c.get(f"/api/astrometry/job/{jid}").json()["stage_text"])
        if "Re-attached to astrometry.net submission 7" in texts:
            break
        time.sleep(0.005)
    env.c.post(f"/api/astrometry/job/{jid}/cancel")
    env.finish(jid)
    assert "Re-attached to astrometry.net submission 7" in texts


def test_cancel_keeps_submission_stops_polling_and_rerun_reattaches(env):
    env.fake.script["submission"] = [ok({"jobs": []})]
    jid = env.run()
    for _ in range(300):
        if env.app.state.astro_jobs.get(jid).nova_subid == 7:
            break
        time.sleep(0.005)
    env.c.post(f"/api/astrometry/job/{jid}/cancel")
    j = env.finish(jid)
    assert j["state"] == "cancelled" and j["resumable"] is True
    n = len(env.fake.calls)
    time.sleep(0.2)
    assert len(env.fake.calls) == n
    env.fake.script["submission"] = [ok({"jobs": [9]})]
    assert env.finish(env.run())["state"] == "done"
    assert env.fake.calls.count("upload") == 1


def test_resume_repolls_same_subid_without_upload(env):
    env.fake.script["submission"] = [ok({"jobs": []})]
    first = env.finish(env.run())
    assert first["resumable"]
    env.fake.script["submission"] = [ok({"jobs": [9]})]
    ups = env.fake.calls.count("upload")
    logins = env.fake.calls.count("login")
    r = env.c.post(f"/api/astrometry/job/{first['job_id']}/resume", json={"apikey": KEY})
    assert r.status_code == 202 and r.json()["job_id"] != first["job_id"]
    j = env.finish(r.json()["job_id"])
    assert j["state"] == "done" and "result" in j
    assert env.fake.calls.count("upload") == ups
    assert env.fake.calls.count("login") == logins + 1       # logs in again
    dump = str(env.app.state.astro_jobs.get(j["job_id"]).__dict__)
    assert KEY not in str(j) and KEY not in dump


def test_resume_errors(env):
    c = env.c
    assert c.post("/api/astrometry/job/nope/resume", json={"apikey": KEY}).status_code == 404
    env.fake.script["upload"] = [Resp(500)]
    j = env.finish(env.run())
    assert c.post(f"/api/astrometry/job/{j['job_id']}/resume", json={"apikey": KEY}
                  ).status_code == 422                     # no submission -> not resumable
    env.fake.script["submission"] = [ok({"jobs": []})]
    env.fake.script["upload"] = [ok({"status": "success", "subid": 7})]
    t = env.finish(env.run(detections=_dets(11)))           # a different field
    assert t["resumable"]
    r = c.post(f"/api/astrometry/job/{t['job_id']}/resume", json={})
    assert r.status_code == 422 and r.json()["detail"] == "API key missing"
    # a running job -> 409
    env.fake.script["submission"] = [ok({"jobs": []})]
    env.clk.t = 0
    jid = c.post(f"/api/astrometry/job/{t['job_id']}/resume", json={"apikey": KEY}).json()["job_id"]
    r = c.post(f"/api/astrometry/job/{t['job_id']}/resume", json={"apikey": KEY})
    assert r.status_code == 409
    c.post(f"/api/astrometry/job/{jid}/cancel")
    env.finish(jid)


def test_done_fingerprint_returns_instantly_with_no_nova_calls(env):
    first = env.finish(env.run())
    assert first["state"] == "done"
    n = len(env.fake.calls)
    again = env.finish(env.run())
    assert again["state"] == "done" and again["result"] == first["result"]
    assert len(env.fake.calls) == n


def test_edited_detections_upload_again(env):
    env.finish(env.run())
    dets = _dets()
    dets[3]["xs"] += 4.0                                    # a star streak moved
    dets[3]["xe"] += 4.0
    assert env.finish(env.run(detections=dets))["state"] == "done"
    assert env.fake.calls.count("upload") == 2


def test_target_edit_alone_reuses_the_solution(env):
    first = env.finish(env.run())
    dets = _dets()
    dets[0]["x"] = 260.5
    again = env.finish(env.run(detections=dets))
    assert again["state"] == "done" and env.fake.calls.count("upload") == 1
    assert again["result"]["ra_deg"] != first["result"]["ra_deg"]


def test_changed_hint_scale_uploads_again(env):
    env.finish(env.run())
    assert env.finish(env.run(focal_mm=1000.0))["state"] in ("done", "failed")
    assert env.fake.calls.count("upload") == 2


def test_memory_is_bounded_to_the_last_20_jobs(env):
    for i in range(24):
        dets = _dets()
        dets[1]["xs"] += i + 1
        env.finish(env.run(detections=dets))
    assert len(env.app.state.astro_jobs._jobs) <= 20


# -- log hygiene --------------------------------------------------------------------------
def test_logs_name_steps_but_never_key_or_session(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.fake.script["calibration"] = [Resp(409), ok(CAL)]
    j = env.finish(env.run())
    assert j["state"] == "done"
    text = caplog.text
    assert "nova step=login" in text and "nova step=upload" in text
    assert "nova step=calibration attempt=1/4 result=409" in text
    assert "subid=7" in text and "jobid=9" in text
    assert KEY not in text and SESSION not in text and "nova.astrometry.net" not in text
    # an exception path
    env.fake.script["submission"] = [requests.ConnectionError(f"boom {KEY} {SESSION}")]
    env.finish(env.run(detections=_dets(13)))
    assert KEY not in caplog.text and SESSION not in caplog.text
    assert "ConnectionError" in caplog.text

