"""
analysis/astrometry/jobs.py  (T5.2b)
====================================
In-memory background-job registry for the nova solve. Ported from ssa-toolkit
``data_api/routes/astrometry_jobs.py`` (states, single-active policy, cooperative cancel,
daemon worker thread, ``to_dict``), trimmed to this flow: no EDGES passes, no nova ids in
the output. Jobs are lost on an analysis restart. The API key is never part of a job: the
worker closure holds the nova client, the job holds only state / stage / result / error.

``to_dict`` -> ``{job_id, state, stage_text, started, result?, error?}``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("analysis.astrometry.jobs")

UPLOADING = "uploading"
QUEUED = "queued"
SOLVING = "solving"
MAPPING = "mapping"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TIMEOUT = "timeout"

ACTIVE_STATES = {UPLOADING, QUEUED, SOLVING, MAPPING}
STAGE_TEXT = {
    UPLOADING: "Uploading to astrometry.net",
    QUEUED: "Queued at astrometry.net",
    SOLVING: "Solving",
    MAPPING: "Mapping the target",
    DONE: "Solved",
    FAILED: "Failed",
    CANCELLED: "Cancelled",
    TIMEOUT: "Timed out",
}
MAX_KEPT = 20


class JobCancelled(Exception):
    """Raised at a cancel checkpoint so the registry records CANCELLED, not FAILED."""


class JobError(Exception):
    """A readable failure. ``state`` is FAILED or TIMEOUT."""

    def __init__(self, text: str, state: str = FAILED):
        super().__init__(text)
        self.text = text
        self.state = state


@dataclass
class Job:
    id: str
    state: str = UPLOADING
    started: float = field(default_factory=time.time)
    result: Optional[dict] = None
    error: Optional[str] = None
    stage_text: str = STAGE_TEXT[UPLOADING]
    nova_subid: Optional[int] = None     # the astrometry.net submission (kept after any end)
    nova_jobid: Optional[int] = None
    fingerprint: Optional[str] = None    # (image, detections used, hint scale) - see api.py
    unsolvable: bool = False             # astrometry.net itself said "could not solve"
    no_resume: bool = False              # a re-poll cannot help (scale mismatch, bad WCS)
    spec: Optional[dict] = None          # what a resume needs (never the key)
    solution: Optional[object] = None    # the SolveResult once nova answered
    _cancel: bool = False

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def cancelled(self) -> bool:
        return self._cancel

    def check_cancel(self) -> None:
        if self._cancel:
            raise JobCancelled()

    def set_state(self, state: str, text: Optional[str] = None) -> None:
        self.state = state
        self.stage_text = text or STAGE_TEXT.get(state, state)

    @property
    def resumable(self) -> bool:
        return (self.state in (FAILED, TIMEOUT, CANCELLED) and self.nova_subid is not None
                and not self.unsolvable and not self.no_resume)

    def to_dict(self) -> dict:
        d = {"job_id": self.id, "state": self.state, "stage_text": self.stage_text,
             "started": self.started, "resumable": self.resumable}
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
        return d


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> Optional[Job]:
        with self._lock:
            return next((j for j in self._jobs.values() if j.active), None)

    def latest(self) -> Optional[Job]:
        """The active job, else the most recently started finished one, else None."""
        with self._lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            if j.active:
                return j
        return max(jobs, key=lambda j: j.started) if jobs else None

    def cancel(self, job_id: str) -> Optional[Job]:
        with self._lock:
            j = self._jobs.get(job_id)
        if j and j.active:
            j._cancel = True
        return j

    def find_reusable(self, fingerprint: str) -> Optional[Job]:
        """The newest job with this fingerprint that kept a submission or a solution and was
        not declared unsolvable by astrometry.net."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)
        return next((j for j in jobs if j.fingerprint == fingerprint and not j.unsolvable
                     and (j.solution is not None or j.nova_subid is not None)), None)

    def create_and_run(self, worker: Callable[[Job], dict],
                       init: Optional[Callable[[Job], None]] = None) -> Optional[Job]:
        """Register a job and run ``worker(job)`` on a daemon thread; None when one is
        already active (single-active policy)."""
        with self._lock:
            if any(j.active for j in self._jobs.values()):
                return None
            job = Job(id=uuid.uuid4().hex)
            if init:
                init(job)
            self._jobs[job.id] = job
            for old in sorted((j for j in self._jobs.values() if not j.active),
                              key=lambda j: j.started)[:max(0, len(self._jobs) - MAX_KEPT)]:
                del self._jobs[old.id]
        threading.Thread(target=self._run, args=(job, worker), daemon=True).start()
        return job

    def _run(self, job: Job, worker: Callable[[Job], dict]) -> None:
        try:
            result = worker(job)
        except JobCancelled:
            job.set_state(CANCELLED)
        except JobError as e:
            job.error = e.text
            job.set_state(e.state)
        except Exception:  # noqa: BLE001 - never surface exception text
            log.warning("astrometry job %s failed unexpectedly", job.id, exc_info=True)
            job.error = "astrometry.net could not solve this frame"
            job.set_state(FAILED)
        else:
            if job._cancel:
                job.set_state(CANCELLED)
            else:
                job.result = result
                job.set_state(DONE)
