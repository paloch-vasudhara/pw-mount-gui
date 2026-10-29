"""TRACK OBJECT verdict service (T4.1): TLE fetch + pass maths + the rule table, with the pass
cache (the maths is pure, `passes.py`). One instance per gateway, called from the check
endpoint's worker thread."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .passes import PassInfo, compute_pass, refresh_now, track_verdict
from .tle import (
    CODE_TLE_UNAVAILABLE,
    R_TLE_UNAVAILABLE,
    TleData,
    TleError,
    TleSource,
)
from .trackplan import SunFn, TrackMount, TrackPlan, TrackRefusal, TrackSetup, plan_track

PASS_TTL_S = 60.0  # a cached pass is recomputed at least this often (new TLE, Lim EL, drift)
CACHE_MAX = 64
PLAN_TTL_S = 10.0  # a cached track plan (row 7) is replanned at least this often
PLAN_POSE_TOL_DEG = 0.5  # ... and when the mount has moved this far on either axis
R_NO_TIMESCALE = "Ephemeris unavailable"


@dataclass
class TrackCheck:
    """What the check endpoint reports for `track_object`: `reason` None = allowed."""

    reason: str | None
    code: str | None = None
    target: tuple[float, float] | None = None  # the object's (alt, az) now
    pass_info: dict[str, Any] | None = None  # PassInfo fields + `phase`
    tle_age_days: float | None = None
    notes: list[str] = field(default_factory=list)  # e.g. the near-zenith note
    detour: dict[str, Any] | None = None  # {"seconds", "count"} of the planned Sun detours
    # T4.2c: what the executor flies (not part of the check endpoint's body)
    plan: TrackPlan | None = None
    setup: TrackSetup | None = None
    tle: TleData | None = None


class TrackChecker:
    def __init__(
        self,
        tle: TleSource,
        timescale: Callable[[], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.tle = tle
        self.timescale = timescale  # () -> skyfield timescale | None
        self.clock = clock  # Unix seconds (a test seam)
        self._lock = threading.Lock()
        # (norad, line1, line2, site, lim_el) -> (computed_at, PassInfo)
        self._cache: dict[tuple, tuple[float, PassInfo]] = {}
        # same key -> (planned_at, mount, TrackPlan | TrackRefusal): row 7, cached with the pass
        self._plans: dict[tuple, tuple[float, TrackMount, TrackPlan | TrackRefusal]] = {}

    def evaluate(
        self,
        norad: int,
        site: tuple[float, float, float],
        lim_el: float,
        sun_fn: Callable[[], tuple[float, float] | None],
        mount: TrackMount | None = None,
        sun_at: SunFn | None = None,
        fresh: bool = False,
    ) -> TrackCheck:
        """Rules 1-6 (row 0, the mount guards, is the caller's); row 7 (the track plan: rendezvous,
        Sun detours) when the mount pose / limits and the Sun-at-time function are given.
        `fresh` skips the plan cache (the executor's re-plan after the rendezvous slew)."""
        try:
            tle = self.tle.get(norad)
        except TleError as exc:
            return TrackCheck(exc.reason, exc.code)
        ts = self.timescale()
        if ts is None:
            return TrackCheck(R_NO_TIMESCALE, "ephemeris_unavailable")
        now = self.clock()
        try:
            info = self._pass(tle, site, lim_el, now, sun_fn, ts)
        except Exception:  # sgp4 failure (decayed / bad elements), malformed lines
            return TrackCheck(R_TLE_UNAVAILABLE, CODE_TLE_UNAVAILABLE)
        reason, code, phase = track_verdict(info, now, lim_el)
        plan: TrackPlan | None = None
        setup: TrackSetup | None = None
        if reason is None and mount is not None and sun_at is not None:
            setup = TrackSetup(tle.line1, tle.line2, site, lim_el, ts, sun_at, mount)
            res = self._plan(tle, site, lim_el, now, info, mount, sun_at, ts, fresh)
            if isinstance(res, TrackRefusal):
                reason, code, phase = res.reason, res.code, None
            else:
                plan = res
        body = asdict(info)
        body["phase"] = phase
        body["t_r"] = plan.t_r if plan is not None else None
        detour = None
        if plan is not None and plan.detours:
            detour = {"seconds": round(plan.detour_seconds, 1), "count": len(plan.detours)}
        return TrackCheck(
            reason,
            code,
            (info.alt, info.az),
            body,
            tle.age_at(self.tle.clock()),
            list(plan.notes) if plan is not None else [],
            detour,
            plan,
            setup if plan is not None else None,
            tle,
        )

    def _plan(
        self,
        tle: TleData,
        site: tuple[float, float, float],
        lim_el: float,
        now: float,
        info: PassInfo,
        mount: TrackMount,
        sun_at: SunFn,
        ts: Any,
        fresh: bool = False,
    ) -> TrackPlan | TrackRefusal:
        key = (tle.norad, tle.line1, tle.line2, site, lim_el)
        with self._lock:
            hit = None if fresh else self._plans.get(key)
        if hit is not None:
            at, m, res = hit
            same_pose = (
                abs(m.axis0 - mount.axis0) < PLAN_POSE_TOL_DEG
                and abs(m.axis1 - mount.axis1) < PLAN_POSE_TOL_DEG
            )
            if 0.0 <= now - at < PLAN_TTL_S and same_pose:
                if isinstance(res, TrackRefusal) or res.t_r >= now:
                    return res
        try:
            res = plan_track(
                TrackSetup(tle.line1, tle.line2, site, lim_el, ts, sun_at, mount), info, now
            )
        except Exception:  # never let the planner break the verdict
            res = TrackRefusal(R_TLE_UNAVAILABLE, CODE_TLE_UNAVAILABLE)
        with self._lock:
            if len(self._plans) >= CACHE_MAX:
                self._plans.clear()
            self._plans[key] = (now, mount, res)
        return res

    def _pass(
        self,
        tle: TleData,
        site: tuple[float, float, float],
        lim_el: float,
        now: float,
        sun_fn: Callable[[], tuple[float, float] | None],
        ts: Any,
    ) -> PassInfo:
        """The current pass: cached per (object, TLE, site, Lim EL) until its set passes or
        PASS_TTL_S go by; a cached pass only has its live fields (alt / az / sun_sep) refreshed."""
        key = (tle.norad, tle.line1, tle.line2, site, lim_el)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            at, info = hit
            fresh = 0.0 <= now - at < PASS_TTL_S and (info.t_set is None or now < info.t_set)
            if fresh:
                live = refresh_now(info, tle.line1, tle.line2, site, now, sun_fn, ts)
                # a pass-less entry (the object was below the horizon) is stale once it rises
                if info.peak_alt is not None or live.alt < 0.0:
                    return live
        info = compute_pass(tle.line1, tle.line2, site, lim_el, now, sun_fn, ts)
        with self._lock:
            if len(self._cache) >= CACHE_MAX:
                self._cache.clear()
            self._cache[key] = (now, info)
        return info
