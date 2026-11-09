"""
analysis/astrometry/nova.py  (T5.2b)
====================================
The nova.astrometry.net client. Ported (nova path only, no ASTAP) from ssa-toolkit
``photometry/platesolve.py``: ``_anet_login`` / ``_anet_submit`` / ``_anet_wait`` /
``_anet_fetch_wcs`` / ``solve_astrometry_net`` / ``verify_astrometry_net_key`` and the
``NovaBackend`` wrapper in ``photometry/solve.py``.

Differences from the source:
  * every failure becomes a :class:`NovaError` with a fixed ``kind`` - never the
    exception text (it can carry URLs / response bodies); the API key is only ever
    POSTed in the login body and is never logged;
  * the WCS download is mandatory (the mapping needs it), not best-effort;
  * the client is an injectable object: ``factory(apikey) -> client`` with
    ``client.solve(image_path, ...) -> NovaSolution`` (see ``app.state.nova_factory``);
    tests and the browser gate swap in :class:`FakeNova`;
  * resilience: every request is retried on a transient failure (connection error,
    timeout, HTTP 409 / 429 / 500 / 502 / 503 / 504) after 2, 5, 10 s - except the upload,
    which is retried only when no response came back or on 429 / 503 so that a retry can
    never create a second submission; a failed poll does not end the job; a successful job
    whose results are not ready yet is re-fetched for up to 30 s; ``resume_subid`` re-polls
    an earlier submission instead of uploading again.

Failure kinds: ``invalid_key`` | ``unreachable`` | ``busy`` | ``failed`` | ``timeout`` (a
cancel is raised by the ``check_cancel`` hook itself and passes through untouched).
"""

from __future__ import annotations

import io
import json
import logging
import math
import numbers
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger("analysis.astrometry")

ANET_BASE = "https://nova.astrometry.net/api"
ANET_SITE = "https://nova.astrometry.net"   # wcs_file lives at the ROOT, not /api

SOLVE_TIMEOUT_S = 1800.0   # slow is not failed: 30 min wall clock
POLL_S = 5.0
REQUEST_TIMEOUT_S = 60.0

INVALID_KEY = "invalid_key"
UNREACHABLE = "unreachable"
FAILED = "failed"
TIMED_OUT = "timeout"
BUSY = "busy"                  # a transient HTTP status that outlasted the retries

RETRY_STATUSES = frozenset({409, 429, 500, 502, 503, 504})
UPLOAD_RETRY_STATUSES = frozenset({429, 503})   # a retry must never create a 2nd submission
RETRY_WAITS_S = (2.0, 5.0, 10.0)                # waits before retry 1, 2, 3
MAX_POLL_FAILURES = 5                           # consecutive failed polls that end the job
RESULTS_WAIT_S = 30.0                           # "job succeeded but results not ready" budget
RESULTS_RETRY_S = 3.0


class NovaError(Exception):
    """A nova failure. ``kind`` is one of the constants above; the message is fixed text.
    ``status`` = the last HTTP status behind a ``busy`` failure; ``retries`` = retries spent;
    ``unsolvable`` = nova itself reported that it could not solve this field."""

    def __init__(self, kind: str, status: Optional[int] = None, retries: int = 0,
                 unsolvable: bool = False):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.retries = retries
        self.unsolvable = unsolvable


@dataclass
class NovaSolution:
    ra_deg: float
    dec_deg: float
    orientation_deg: float
    pixscale_arcsec: float
    parity: int
    wcs_header: dict = field(default_factory=dict)   # CTYPE/CRVAL/CRPIX/CD/SIP scalars


class NovaClient(Protocol):
    def solve(self, image_path: str, *, width: int, height: int,
              scale_est_arcsec: Optional[float], timeout: float,
              progress: Optional[Callable], check_cancel: Optional[Callable],
              resume_subid: Optional[int] = None) -> NovaSolution: ...


def _jsonable_header(hdr) -> dict:
    """FITS header -> dict of primitives (ssa ``_jsonable_header``)."""
    out: dict[str, Any] = {}
    for k in hdr.keys():
        if not k or k in ("COMMENT", "HISTORY"):
            continue
        try:
            v = hdr[k]
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, bool):
            out[k] = bool(v)
        elif isinstance(v, str):
            out[k] = str(v)
        elif isinstance(v, numbers.Integral):
            out[k] = int(v)
        elif isinstance(v, numbers.Real):
            out[k] = float(v)
    return out


def _cancellable_sleep(seconds: float, check_cancel, step: float = 1.0) -> None:
    end = time.monotonic() + seconds
    while True:
        if check_cancel:
            check_cancel()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


class _NotReady(Exception):
    """Calibration / wcs of a successful job are not usable yet (internal)."""


class NovaHttpClient:
    """The real client (``requests``). Tests inject ``http=`` (a scripted session) and
    ``sleep=`` (instant waits); they never touch the network."""

    def __init__(self, apikey: str, http=None, poll_s: float = POLL_S,
                 request_timeout: float = REQUEST_TIMEOUT_S, waits=RETRY_WAITS_S,
                 sleep: Optional[Callable] = None, max_poll_failures: int = MAX_POLL_FAILURES,
                 results_wait_s: float = RESULTS_WAIT_S,
                 results_retry_s: float = RESULTS_RETRY_S, clock: Callable = time.monotonic):
        self._apikey = apikey
        self._clock = clock
        self._http = http
        self.poll_s = poll_s
        self.request_timeout = request_timeout
        self._waits = tuple(waits)
        self._sleep = sleep or _cancellable_sleep      # sleep(seconds, check_cancel)
        self.max_poll_failures = max_poll_failures
        self.results_wait_s = results_wait_s
        self.results_retry_s = results_retry_s
        self._subid: Optional[int] = None
        self._jobid: Optional[int] = None

    def _note(self, step: str, attempt: int, total: int, outcome) -> None:
        # step, attempt, status / exception class and ids only: never the key, the session
        # token, a URL or a body
        log.info("nova step=%s attempt=%d/%d result=%s subid=%s jobid=%s",
                 step, attempt, total, outcome, self._subid, self._jobid)

    def _request(self, http, step: str, method: str, url: str, check_cancel=None,
                 retry_statuses=RETRY_STATUSES, **kw):
        """One logical request with retries. Returns a response whose status is not
        retryable (the caller judges it); raises ``NovaError`` busy / unreachable."""
        import requests
        total = len(self._waits) + 1
        for attempt in range(1, total + 1):
            last = attempt == total
            try:
                resp = getattr(http, method)(url, timeout=self.request_timeout, **kw)
            except (requests.RequestException, OSError) as e:
                self._note(step, attempt, total, type(e).__name__)
                if last:
                    raise NovaError(UNREACHABLE, retries=attempt - 1) from None
            else:
                self._note(step, attempt, total, resp.status_code)
                if resp.status_code not in retry_statuses:
                    return resp
                if last:
                    raise NovaError(BUSY, status=resp.status_code, retries=attempt - 1)
            self._sleep(self._waits[attempt - 1], check_cancel)
        raise NovaError(UNREACHABLE)   # pragma: no cover - the loop always returns / raises

    @staticmethod
    def _json(resp) -> dict:
        if resp.status_code != 200:
            raise NovaError(BUSY, status=resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise NovaError(BUSY, status=resp.status_code) from None
        if not isinstance(data, dict):
            raise NovaError(BUSY, status=resp.status_code)
        return data

    def _login(self, http, check_cancel=None) -> str:
        resp = self._request(http, "login", "post", f"{ANET_BASE}/login", check_cancel,
                             data={"request-json": json.dumps({"apikey": self._apikey})})
        if resp.status_code in (401, 403):
            raise NovaError(INVALID_KEY)
        data = self._json(resp)
        if data.get("status") != "success" or not data.get("session"):
            raise NovaError(INVALID_KEY)
        return data["session"]

    def _submit(self, http, session: str, path: str, scale_est: Optional[float],
                check_cancel=None) -> int:
        params: dict[str, Any] = {
            "session": session, "allow_commercial_use": "n", "allow_modifications": "n",
            "publicly_visible": "n", "downsample_factor": 2,
        }
        if scale_est is not None:
            params.update({"scale_units": "arcsecperpix", "scale_est": scale_est,
                           "scale_err": 25.0})
        with open(path, "rb") as fh:
            # only "no response" and 429 / 503 are retried: any other status may mean the
            # submission was created, so a retry could double-submit
            resp = self._request(http, "upload", "post", f"{ANET_BASE}/upload", check_cancel,
                                 retry_statuses=UPLOAD_RETRY_STATUSES,
                                 data={"request-json": json.dumps(params)},
                                 files={"file": ("field.fits", fh, "application/octet-stream")})
        data = self._json(resp)
        if data.get("status") != "success":
            raise NovaError(FAILED)
        return int(data["subid"])

    def _poll_once(self, http, subid: int, progress, check_cancel) -> Optional[int]:
        """-> the successful jobid, or None (keep polling); raises ``NovaError`` for a failed
        poll (busy / unreachable) or when nova reports every job failed."""
        sub = self._json(self._request(http, "submission", "get",
                                       f"{ANET_BASE}/submissions/{subid}", check_cancel))
        jobids = [j for j in (sub.get("jobs") or []) if j]
        if not jobids:
            if progress:
                progress(stage="queued", subid=subid)
            return None
        self._jobid = jobids[0]
        if progress:
            progress(stage="solving", jobid=jobids[0])
        statuses = {}
        for jid in jobids:
            st = self._json(self._request(http, "job_status", "get",
                                          f"{ANET_BASE}/jobs/{jid}", check_cancel)).get("status")
            if st == "success":
                return int(jid)
            statuses[jid] = st
        if statuses and all(s in ("failure", "error") for s in statuses.values()):
            raise NovaError(FAILED, unsolvable=True)
        return None

    def _wait(self, http, subid: int, timeout: float, progress, check_cancel) -> int:
        t0 = self._clock()
        bad = 0
        while self._clock() - t0 < timeout:
            if check_cancel:
                check_cancel()
            try:
                jobid = self._poll_once(http, subid, progress, check_cancel)
            except NovaError as e:
                if e.kind not in (BUSY, UNREACHABLE):
                    raise
                bad += 1
                if bad >= self.max_poll_failures:
                    raise
            else:
                bad = 0
                if jobid is not None:
                    return jobid
            self._sleep(self.poll_s, check_cancel)
        raise NovaError(TIMED_OUT)

    def _fetch_once(self, http, jobid: int, check_cancel) -> NovaSolution:
        cal = self._request(http, "calibration", "get",
                            f"{ANET_BASE}/jobs/{jobid}/calibration", check_cancel)
        if cal.status_code != 200:
            raise NovaError(BUSY, status=cal.status_code)
        wcs = None
        try:
            cal = cal.json()
            wcs = self._request(http, "wcs_file", "get", f"{ANET_SITE}/wcs_file/{jobid}",
                                check_cancel)
            if wcs.status_code != 200 or not wcs.content:
                raise NovaError(BUSY, status=wcs.status_code)
            from astropy.io import fits
            try:
                with fits.open(io.BytesIO(wcs.content)) as hdul:
                    hdr = _jsonable_header(hdul[0].header)
            except Exception:  # noqa: BLE001
                raise NovaError(FAILED) from None
            return NovaSolution(
                ra_deg=float(cal["ra"]), dec_deg=float(cal["dec"]),
                orientation_deg=float(cal["orientation"]) % 360.0,
                pixscale_arcsec=float(cal["pixscale"]),
                parity=int(math.copysign(1.0, cal.get("parity", -1.0))), wcs_header=hdr)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise _NotReady() from None

    def _fetch(self, http, jobid: int, check_cancel) -> NovaSolution:
        """The job reported success; its results may lag: retry for up to ``results_wait_s``."""
        waited = 0.0
        while True:
            try:
                return self._fetch_once(http, jobid, check_cancel)
            except (NovaError, _NotReady) as e:
                if isinstance(e, NovaError) and e.kind not in (BUSY, UNREACHABLE):
                    raise
                if waited >= self.results_wait_s:
                    if isinstance(e, _NotReady):
                        raise NovaError(FAILED) from None
                    raise
            self._sleep(self.results_retry_s, check_cancel)
            waited += self.results_retry_s

    def solve(self, image_path, *, width, height, scale_est_arcsec, timeout=SOLVE_TIMEOUT_S,
              progress=None, check_cancel=None, resume_subid=None) -> NovaSolution:
        import requests
        try:
            with (self._http or requests.Session()) as http:
                if progress:
                    progress(stage="uploading")
                session = self._login(http, check_cancel)
                if resume_subid is not None:
                    subid = int(resume_subid)            # log in again, but never re-upload
                else:
                    subid = self._submit(http, session, image_path, scale_est_arcsec,
                                         check_cancel)
                self._subid = subid
                if progress:
                    progress(stage="queued", subid=subid)
                jobid = self._wait(http, subid, timeout, progress, check_cancel)
                self._jobid = jobid
                return self._fetch(http, jobid, check_cancel)
        except NovaError:
            raise
        except (requests.RequestException, OSError):
            raise NovaError(UNREACHABLE) from None
        except (ValueError, KeyError, TypeError):
            raise NovaError(FAILED) from None


def verify_key(apikey: str, timeout: float = 20.0) -> bool:
    """Login-only key check (ssa ``verify_astrometry_net_key``). Not used by the routes."""
    import requests
    try:
        r = requests.post(f"{ANET_BASE}/login",
                          data={"request-json": json.dumps({"apikey": apikey.strip()})},
                          timeout=timeout)
        d = r.json()
        return d.get("status") == "success" and bool(d.get("session"))
    except Exception:  # noqa: BLE001
        return False


def real_factory(apikey: str) -> NovaClient:
    return NovaHttpClient(apikey)


# -- deterministic fake (PW_ASTROMETRY_FAKE=1, tests, the browser gate) ---------------
FAKE_CENTER = (150.0, 20.0)   # RA, Dec of the image centre
FAKE_SCALE = 5.0              # arcsec / px when there is no hint


class FakeNova:
    """No network. Walks uploading -> queued -> solving over ``delay_s`` (checking
    cancel) and returns a fixed north-up TAN WCS centred on the image."""

    def __init__(self, delay_s: float = 3.0, step_s: float = 0.25):
        self.delay_s = delay_s
        self.step_s = step_s

    def solve(self, image_path, *, width, height, scale_est_arcsec, timeout=SOLVE_TIMEOUT_S,
              progress=None, check_cancel=None, resume_subid=None) -> NovaSolution:
        scale = float(scale_est_arcsec) if scale_est_arcsec else FAKE_SCALE
        override = os.environ.get("PW_ASTROMETRY_FAKE_SCALE")   # tests / browser gate: a mismatch
        if override:
            try:
                scale = float(override)
            except ValueError:
                pass
        stages = [("uploading", {}), ("queued", {"subid": 1}), ("solving", {"jobid": 1})]
        for stage, kw in stages:
            if progress:
                progress(stage=stage, **kw)
            _cancellable_sleep(self.delay_s / len(stages), check_cancel, self.step_s)
        d = scale / 3600.0
        hdr = {
            "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN",
            "CRVAL1": FAKE_CENTER[0], "CRVAL2": FAKE_CENTER[1],
            "CRPIX1": width / 2.0 + 0.5, "CRPIX2": height / 2.0 + 0.5,
            "CD1_1": -d, "CD1_2": 0.0, "CD2_1": 0.0, "CD2_2": d,
            "WCSAXES": 2, "EQUINOX": 2000.0,
        }
        return NovaSolution(FAKE_CENTER[0], FAKE_CENTER[1], 0.0, scale, -1, hdr)


def fake_factory(apikey: str) -> NovaClient:   # noqa: ARG001 - the key is ignored
    return FakeNova()
