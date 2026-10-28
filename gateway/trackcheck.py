"""TRACK OBJECT verdict service (T4.1): TLE fetch + pass maths + the rule table, with the pass
cache (the maths is pure, `passes.py`). One instance per gateway, called from the check
endpoint's worker thread."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from .passes import PassInfo, compute_pass, refresh_now, track_verdict
from .tle import (
    CODE_TLE_UNAVAILABLE,
    R_TLE_UNAVAILABLE,
    TleData,
    TleError,
    TleSource,
)

PASS_TTL_S = 60.0  # a cached pass is recomputed at least this often (new TLE, Lim EL, drift)
CACHE_MAX = 64
R_NO_TIMESCALE = "Ephemeris unavailable"


@dataclass
class TrackCheck:
    """What the check endpoint reports for `track_object`: `reason` None = allowed."""

    reason: str | None
    code: str | None = None
    target: tuple[float, float] | None = None  # the object's (alt, az) now
    pass_info: dict[str, Any] | None = None  # PassInfo fields + `phase`
    tle_age_days: float | None = None


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

    def evaluate(
        self,
        norad: int,
        site: tuple[float, float, float],
        lim_el: float,
        sun_fn: Callable[[], tuple[float, float] | None],
    ) -> TrackCheck:
        """Rules 1-6 (row 0, the mount guards, is the caller's)."""
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
        body = asdict(info)
        body["phase"] = phase
        return TrackCheck(
            reason,
            code,
            (info.alt, info.az),
            body,
            tle.age_at(self.tle.clock()),
        )

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
