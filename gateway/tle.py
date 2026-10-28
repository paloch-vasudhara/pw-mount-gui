"""TLE client (T4.1, Q13): the gateway fetches the TLE of a tracked object from the analysis
service by NORAD id; the browser only ever sends the id. Sync httpx (the check endpoint runs in
a worker thread), injectable transport for tests.

Cache: 10 min per NORAD; a 404 is remembered for 30 s; a failure (timeout, connection error,
5xx, a malformed answer) is never cached so the next 1 Hz check retries.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

TLE_TIMEOUT_S = 2.0
TLE_TTL_S = 600.0
TLE_MISSING_TTL_S = 30.0

R_TLE_MISSING = "Satellite not found or decayed"
R_TLE_UNAVAILABLE = "Satellite data unavailable"
CODE_TLE_MISSING = "tle_missing"
CODE_TLE_UNAVAILABLE = "tle_unavailable"


class TleError(Exception):
    """The TLE could not be had: `reason` (G7 wording) and a stable `code`."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class TleData:
    norad: int
    name: str | None
    line1: str
    line2: str
    epoch: str | None  # ISO text as the analysis service reports it
    age_days: float | None  # the TLE's age when it was fetched
    fetched: float  # clock() at the fetch (the age grows while the entry is cached)

    def age_at(self, now: float) -> float | None:
        if self.age_days is None:
            return None
        return round(self.age_days + max(0.0, now - self.fetched) / 86400.0, 3)


class TleSource:
    def __init__(
        self,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = TLE_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(transport=transport, timeout=timeout)
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, TleData | TleError]] = {}

    def close(self) -> None:
        self._client.close()

    def get(self, norad: int) -> TleData:
        """The TLE of `norad` (cached). Raises TleError."""
        now = self.clock()
        with self._lock:
            hit = self._cache.get(norad)
        if hit is not None and now < hit[0]:
            if isinstance(hit[1], TleError):
                raise hit[1]
            return hit[1]
        data = self._fetch(norad, now)  # raises (uncached) on a failure
        with self._lock:
            self._cache[norad] = (now + TLE_TTL_S, data)
        return data

    def _fetch(self, norad: int, now: float) -> TleData:
        try:
            r = self._client.get(f"{self.base_url}/api/sats/{norad}")
        except httpx.HTTPError:
            raise TleError(R_TLE_UNAVAILABLE, CODE_TLE_UNAVAILABLE) from None
        if r.status_code == 404:
            err = TleError(R_TLE_MISSING, CODE_TLE_MISSING)
            with self._lock:
                self._cache[norad] = (now + TLE_MISSING_TTL_S, err)
            raise err
        if r.status_code != 200:
            raise TleError(R_TLE_UNAVAILABLE, CODE_TLE_UNAVAILABLE)
        try:
            row: Any = r.json()
            line1, line2 = row["tle_line1"], row["tle_line2"]
            if not (isinstance(line1, str) and isinstance(line2, str) and line1 and line2):
                raise ValueError("tle lines")
            age = row.get("tle_age_days")
            return TleData(
                norad,
                row.get("name"),
                line1,
                line2,
                row.get("tle_epoch"),
                float(age) if isinstance(age, int | float) else None,
                now,
            )
        except (ValueError, KeyError, TypeError):
            raise TleError(R_TLE_UNAVAILABLE, CODE_TLE_UNAVAILABLE) from None
