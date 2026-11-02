"""
analysis/astrometry/api.py  (new for pw-mount-control, T5.2a)
=============================================================
``/api/astrometry/{image,detect,snap}``. Upload handling follows ssa-toolkit
``data_api/routes/astrometry.py::_spool`` (validate the extension, spool to a temp
file with the right suffix, load, delete the temp file) plus a 200 MB cap.

Errors are readable strings only - never exception text. T5.2b adds ``/run`` and
``/job*`` next to these on the same router (``make_router``) and the same sessions.

T5.2b endpoints (``/run`` is called only by the gateway, which adds ``apikey``):
  POST /api/astrometry/run {image_id, detections[], epoch_iso, exposure_s?, focal_mm?,
       pixel_um?, apikey} -> 202 {job_id, state}; 422 readable; 404 EXPIRED;
       409 "An astrometry solve is already running"
  GET  /api/astrometry/job/{id} -> {job_id, state, stage_text, started, result?, error?}
  GET  /api/astrometry/job      -> the active job, else the latest finished one, else 404
  POST /api/astrometry/job/{id}/resume {apikey} -> 202 {job_id, state}: a NEW job that logs in
       again and re-polls the same nova submission (no upload); 404 unknown; 409 busy;
       422 not resumable / key missing. ``resumable`` is in every job dict.
  POST /api/astrometry/job/{id}/cancel -> the job dict (404 for an unknown id)
The key is used only to build the nova client; it is not stored on the job, logged or returned.

Injection / test switches:
  * ``app.state.nova_factory`` - ``factory(apikey) -> client`` (see ``nova.NovaClient``);
    default is the real nova client. Tests assign a fake after ``create_app()``.
  * env ``PW_ASTROMETRY_FAKE=1`` - ``create_app`` installs ``nova.fake_factory``: a
    deterministic fake that "solves" after ~3 s with a north-up TAN WCS centred on the image
    (RA 150, Dec 20; 5 arcsec/px, or the hint scale when one is sent). For the browser gate
    only - it never touches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from . import jobs as jobs_mod
from . import nova, service
from . import solve as solve_mod
from .io import SUPPORTED_EXTS, load_image
from .jobs import Job, JobError, JobRegistry
from .sessions import ImageSession, ImageSessions

log = logging.getLogger("analysis.astrometry")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
WORK_TIMEOUT_S = 60.0
EXPIRED = "Image expired — load it again"
SOLVE_TIMEOUT_S = nova.SOLVE_TIMEOUT_S
MIN_STREAKS = 10
BUSY = "An astrometry solve is already running"
ERR_KEY = "Invalid astrometry.net API key"
ERR_UNREACHABLE = "astrometry.net unreachable (network)"
ERR_FAILED = "astrometry.net could not solve this frame"
ERR_TIMEOUT = "Solve timed out"
ERR_SCALE = "Solved scale does not match the camera"
ALLOWED_EXTS = SUPPORTED_EXTS - {".fts"}   # the spec's list: fits fit tif tiff jpg jpeg png


class ResumeIn(BaseModel):
    apikey: str | None = Field(default=None, max_length=256)


class DetectIn(BaseModel):
    image_id: str


class SnapIn(BaseModel):
    image_id: str
    x: float = Field(ge=-1e6, le=1e6)
    y: float = Field(ge=-1e6, le=1e6)
    role: Literal["star", "target"]


class RunIn(BaseModel):
    image_id: str
    detections: list[dict] = Field(default_factory=list, max_length=500)
    epoch_iso: str | None = None
    exposure_s: float | None = Field(default=None, ge=0, le=1e5)
    focal_mm: float | None = Field(default=None, gt=0, lt=1e5)
    pixel_um: float | None = Field(default=None, gt=0, lt=1e3)
    apikey: str | None = Field(default=None, max_length=256)


def _complete_streaks(dets: list[dict]) -> list[dict]:
    return [d for d in dets if d.get("role") == "star" and d.get("shape") == "streak"
            and all(isinstance(d.get(k), (int, float)) for k in ("xs", "ys", "xe", "ye"))]


def _validate_run(body: RunIn) -> tuple[str, tuple[float, float], list[tuple[float, float]]]:
    """-> (normalised epoch, target point, star midpoints) or HTTP 422 with a reason."""
    streaks = _complete_streaks(body.detections)
    if len(streaks) < MIN_STREAKS:
        raise HTTPException(422, f"At least {MIN_STREAKS} complete star streaks are needed "
                                 f"({len(streaks)} found)")
    tgt = service.target_point(body.detections)
    if tgt is None:
        raise HTTPException(422, "Mark the target before running")
    epoch = service.normalize_epoch(body.epoch_iso)
    if epoch is None:
        raise HTTPException(422, "Enter the exposure start time")
    return epoch, tgt, service.star_midpoints(streaks)


def _fingerprint(image_id: str, stars, hint_scale) -> str:
    """(image, the star midpoints used for the solve, the hint scale): what nova is shown.
    The target / epoch / exposure are not part of it - they only affect the mapping."""
    pts = sorted((round(float(x), 3), round(float(y), 3)) for x, y in stars)
    hs = None if hint_scale is None else round(float(hint_scale), 4)
    raw = json.dumps([image_id, pts, hs])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _nova_error_text(e: nova.NovaError) -> str:
    if e.kind == nova.INVALID_KEY:
        return ERR_KEY
    if e.kind == nova.UNREACHABLE:
        return ERR_UNREACHABLE
    if e.kind == nova.BUSY:
        base = f"astrometry.net busy (HTTP {e.status})"
        return f"{base} \u2014 retried {e.retries}\u00d7" if e.retries else base
    return ERR_FAILED


def _make_worker(client, *, spec: dict):
    """The job body. ``client`` holds the key; the job never does. ``spec`` is what a
    resume needs (width, height, epoch, exposure_s, target, stars, hint_scale). If the job
    already carries a ``nova_subid`` the submission is re-attached, never re-uploaded; if it
    carries a ``solution`` no nova call is made at all."""
    width, height, epoch = spec["width"], spec["height"], spec["epoch"]
    exposure_s, target, stars, hint_scale = (spec["exposure_s"], spec["target"], spec["stars"],
                                             spec["hint_scale"])

    def work(job: Job) -> dict:
        reattach = job.nova_subid

        def progress(stage=None, subid=None, jobid=None):
            if subid is not None:
                job.nova_subid = subid
            if jobid is not None:
                job.nova_jobid = jobid
            if stage in (jobs_mod.UPLOADING, jobs_mod.QUEUED, jobs_mod.SOLVING):
                if reattach is not None and stage != jobs_mod.SOLVING:
                    job.set_state(stage, f"Re-attached to astrometry.net submission {reattach}")
                else:
                    job.set_state(stage)

        job.check_cancel()
        sol = job.solution
        if sol is None:
            try:
                sol = solve_mod.solve_points(client, stars, width, height, hint_scale,
                                             SOLVE_TIMEOUT_S, progress, job.check_cancel,
                                             resume_subid=reattach)
            except nova.NovaError as e:
                job.unsolvable = e.unsolvable
                if e.kind == nova.TIMED_OUT:
                    raise JobError(ERR_TIMEOUT, jobs_mod.TIMEOUT) from None
                raise JobError(_nova_error_text(e)) from None
            job.solution = sol
        job.check_cancel()
        job.set_state(jobs_mod.MAPPING)
        if not solve_mod.scale_ok(sol.scale_arcsec_px, hint_scale):
            job.no_resume = True
            raise JobError(ERR_SCALE)
        wcs = solve_mod.build_wcs(sol.wcs_header)
        if wcs is None:
            job.no_resume = True
            raise JobError(ERR_FAILED)
        try:
            ra, dec = solve_mod.map_point(wcs, *target)
        except Exception:  # noqa: BLE001
            job.no_resume = True
            raise JobError(ERR_FAILED) from None
        mid_iso, mid_unix, warnings = solve_mod.mid_exposure(epoch, exposure_s)
        return {
            "ra_deg": round(ra, 6), "dec_deg": round(dec, 6),
            "epoch_mid_iso": mid_iso, "epoch_mid_unix": mid_unix,
            "residual_arcsec": round(solve_mod.sigma_arcsec(sol.scale_arcsec_px), 3),
            "scale_arcsec_px": round(sol.scale_arcsec_px, 4),
            "n_stars_used": sol.n_stars_used, "warnings": warnings,
        }

    return work


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _spool(file: UploadFile) -> tuple[str, str]:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(422, "Unsupported file type")
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    n = 0
    try:
        with tmp:
            while chunk := await file.read(1024 * 1024):
                n += len(chunk)
                if n > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Image too large (200 MB maximum)")
                tmp.write(chunk)
    except BaseException:
        _unlink(tmp.name)
        raise
    if n == 0:
        _unlink(tmp.name)
        raise HTTPException(422, "The file is empty")
    return tmp.name, ext


def _load_and_preview(path: str) -> tuple:
    img = load_image(path)
    return img, service.build_meta(img), service.make_preview(img.data)


async def _work(fn, *args):
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=WORK_TIMEOUT_S)
    except TimeoutError:
        raise HTTPException(503, "The image took too long to process") from None


def make_router(sessions: ImageSessions, jobs: JobRegistry | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/astrometry")
    jobs = jobs if jobs is not None else JobRegistry()

    def session(image_id: str) -> ImageSession:
        s = sessions.get(image_id)
        if s is None:
            raise HTTPException(404, EXPIRED)
        return s

    @router.post("/image")
    async def image(file: UploadFile) -> dict:
        path, _ext = await _spool(file)
        try:
            img, meta, preview = await _work(_load_and_preview, path)
        except HTTPException:
            raise
        except Exception:
            log.warning("astrometry image load failed", exc_info=True)
            raise HTTPException(422, "Could not read this image") from None
        finally:
            _unlink(path)
        image_id = sessions.add(img, meta)
        return {"image_id": image_id, "preview": preview, "meta": meta}

    @router.post("/detect")
    async def detect(body: DetectIn) -> dict:
        s = session(body.image_id)
        try:
            return await _work(service.detect, s)
        except HTTPException:
            raise
        except Exception:
            log.warning("astrometry detect failed", exc_info=True)
            raise HTTPException(503, "Streak detection failed on this image") from None

    @router.post("/snap")
    async def snap(body: SnapIn) -> dict:
        s = session(body.image_id)
        try:
            return await _work(service.snap, s, body.x, body.y, body.role)
        except HTTPException:
            raise
        except Exception:
            log.warning("astrometry snap failed", exc_info=True)
            raise HTTPException(503, "Could not measure at that point") from None

    @router.post("/run", status_code=202)
    async def run(body: RunIn, request: Request) -> dict:
        if not body.apikey:
            raise HTTPException(422, "API key missing")
        s = session(body.image_id)
        epoch, target, stars = _validate_run(body)
        factory = getattr(request.app.state, "nova_factory", nova.real_factory)
        hint = solve_mod.hint_scale_arcsec(body.focal_mm, body.pixel_um)
        spec = {"width": int(s.image.width), "height": int(s.image.height), "epoch": epoch,
                "exposure_s": body.exposure_s, "target": target, "stars": stars,
                "hint_scale": hint}
        fp = _fingerprint(body.image_id, stars, hint)
        prev = jobs.find_reusable(fp)

        def init(job: Job) -> None:
            job.fingerprint, job.spec = fp, spec
            if prev is not None:   # same field: re-attach / reuse, never a second upload
                job.nova_subid, job.nova_jobid = prev.nova_subid, prev.nova_jobid
                job.solution = prev.solution

        job = jobs.create_and_run(_make_worker(factory(body.apikey), spec=spec), init)
        if job is None:
            raise HTTPException(409, BUSY)
        return {"job_id": job.id, "state": job.state}

    @router.post("/job/{job_id}/resume", status_code=202)
    async def job_resume(job_id: str, body: ResumeIn, request: Request) -> dict:
        if not body.apikey:
            raise HTTPException(422, "API key missing")
        old = jobs.get(job_id)
        if old is None:
            raise HTTPException(404, "Unknown astrometry job")
        if old.active:
            raise HTTPException(409, BUSY)
        if not old.resumable or old.spec is None:
            raise HTTPException(422, "This astrometry job cannot be resumed")
        factory = getattr(request.app.state, "nova_factory", nova.real_factory)

        def init(job: Job) -> None:
            job.fingerprint, job.spec = old.fingerprint, old.spec
            job.nova_subid, job.nova_jobid, job.solution = (old.nova_subid, old.nova_jobid,
                                                             old.solution)

        job = jobs.create_and_run(_make_worker(factory(body.apikey), spec=old.spec), init)
        if job is None:
            raise HTTPException(409, BUSY)
        return {"job_id": job.id, "state": job.state}

    @router.get("/job")
    async def job_latest() -> dict:
        j = jobs.latest()
        if j is None:
            raise HTTPException(404, "No astrometry job")
        return j.to_dict()

    @router.get("/job/{job_id}")
    async def job_get(job_id: str) -> dict:
        j = jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "Unknown astrometry job")
        return j.to_dict()

    @router.post("/job/{job_id}/cancel")
    async def job_cancel(job_id: str) -> dict:
        j = jobs.cancel(job_id)
        if j is None:
            raise HTTPException(404, "Unknown astrometry job")
        return j.to_dict()

    return router
