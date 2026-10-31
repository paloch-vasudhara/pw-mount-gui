"""Analysis service state: metadata catalogue (API process) + chunk worker (child process).

The API process holds light per-layer metadata only (for `/api/sky/objects`). All SGP4 work runs in
a `ProcessPoolExecutor(max_workers=1)` whose child owns its own catalogue copy (with SatrecArrays),
so the API event loop never computes. Both processes run at BELOW_NORMAL priority on Windows.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor

from .catalog import LayerData, load_layers
from .chunks import STEP_S, Site, compute_chunk, snap_t0
from .db import CatalogUnavailable
from .priority import set_below_normal

log = logging.getLogger("analysis.service")

CACHE_TTL_S = 600.0
REFRESH_S = 6 * 3600.0
ROW_TTL_S = 60.0

# ---- child process -------------------------------------------------------------------------

_child_layers: dict[str, LayerData] = {}
_child_factory: Callable | None = None


def _child_init(factory: Callable) -> None:
    global _child_factory
    set_below_normal()
    _child_factory = factory
    _child_reload()


def _child_reload() -> dict:
    """(Re)load this process's catalogue copy; returns counts only."""
    global _child_layers
    t = time.perf_counter()
    try:
        layers = load_layers(_child_factory(), with_satrecs=True)  # type: ignore[misc]
    except CatalogUnavailable:
        return {"error": "unconfigured"}
    except Exception:
        return {"error": "load-failed"}
    _child_layers = layers
    return {"counts": {k: len(v) for k, v in layers.items()}, "load_s": time.perf_counter() - t}


def _child_chunk(layer: str, site: tuple[float, float, float], t0: int, dur: float) -> bytes:
    data = _child_layers.get(layer)
    if data is None:
        raise RuntimeError("catalogue not loaded")
    return compute_chunk(data, Site(*site), t0, dur, name=layer)


def _warm_state() -> None:
    try:
        from .state import warm

        warm()
    except Exception:
        log.warning("state ephemeris could not be preloaded")


# ---- API process ---------------------------------------------------------------------------


class Unavailable(RuntimeError):
    """Catalogue not ready (HTTP 503)."""


class Service:
    def __init__(self, factory: Callable) -> None:
        self.factory = factory
        self._source_lock = threading.Lock()
        self.source = None  # lazily built in a worker thread (engine creation reads .env)
        self.layers: dict[str, LayerData] = {}
        self.loaded_at: float | None = None
        self.load_error: str | None = None
        self.pool: ProcessPoolExecutor | None = None
        self.cache: dict[tuple, tuple[float, bytes]] = {}
        self.inflight: dict[tuple, asyncio.Future] = {}
        self._bg: set[asyncio.Task] = set()
        self._refresh_lock = asyncio.Lock()
        self.child_ready = False
        self.metrics: dict[str, float | int | str] = {}
        self._rows: dict[int, tuple[float, dict | None]] = {}
        self._countries: list[dict] | None = None

    # ---- lifecycle
    async def start(self) -> None:
        set_below_normal()
        self.pool = ProcessPoolExecutor(
            max_workers=1, initializer=_child_init, initargs=(self.factory,)
        )
        self._spawn(self.refresh())
        self._spawn(self._periodic())
        asyncio.get_running_loop().run_in_executor(None, _warm_state)

    async def stop(self) -> None:
        for t in list(self._bg):
            t.cancel()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_S)
            await self.refresh()

    # ---- catalogue
    def get_source(self):
        with self._source_lock:
            if self.source is None:
                self.source = self.factory()
        return self.source

    def _load_meta(self) -> dict[str, LayerData]:
        return load_layers(self.get_source(), with_satrecs=False)

    async def refresh(self) -> dict:
        """Reload metadata here and the SatrecArrays in the child, without blocking requests."""
        async with self._refresh_lock:
            loop = asyncio.get_running_loop()
            t = time.perf_counter()
            try:
                meta_fut = loop.run_in_executor(None, self._load_meta)
                child_fut = loop.run_in_executor(self.pool, _child_reload)
                layers = await meta_fut
                child = await child_fut
            except CatalogUnavailable:
                self.load_error = "unconfigured"
                return {"ok": False, "error": self.load_error}
            except Exception:
                self.load_error = "load-failed"  # never surface exception text (may hold the DSN)
                log.warning("catalogue refresh failed")
                return {"ok": False, "error": self.load_error}
            if "error" in child:
                self.load_error = child["error"]
                return {"ok": False, "error": self.load_error}
            self.layers = layers
            self.loaded_at = time.time()
            self.load_error = None
            self.child_ready = True
            self.cache.clear()
            self._rows.clear()
            self._countries = None  # recomputed lazily from the reloaded catalogue
            self.metrics = {
                "load_total_s": round(time.perf_counter() - t, 3),
                "child_load_s": round(child["load_s"], 3),
            }
            return {"ok": True, "counts": self.counts()}

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.layers.items()}

    def status(self) -> dict:
        return {
            "ready": self.child_ready,
            "counts": self.counts(),
            "loaded_at": self.loaded_at,
            "error": self.load_error,
            "metrics": self.metrics,
        }

    def require_ready(self) -> None:
        if not self.child_ready:
            if self.load_error == "unconfigured":
                raise Unavailable("satellite database is not configured")
            if self.load_error:
                raise Unavailable("satellite catalogue could not be loaded")
            raise Unavailable("satellite catalogue is loading")

    def tle_row(self, norad: int, fetch: Callable[[int], dict | None]) -> dict | None:
        """Catalogue row (with TLE) for one object, cached 60 s so a 1 Hz poll costs no DB hit."""
        now = time.monotonic()
        hit = self._rows.get(norad)
        if hit and now - hit[0] < ROW_TTL_S:
            return hit[1]
        row = fetch(norad)
        if len(self._rows) > 512:
            self._rows.clear()
        self._rows[norad] = (now, row)
        return row

    def countries(self, fetch: Callable[[], list[dict]]) -> list[dict]:
        """Country list cached with the catalogue (cleared on every reload)."""
        cached = self._countries
        if cached is None:
            cached = self._countries = fetch()
        return cached

    # ---- chunks
    async def chunk(self, layer: str, site: Site, t0_unix: float, duration_s: float) -> bytes:
        self.require_ready()
        step = STEP_S[layer]
        t0 = snap_t0(t0_unix, step)
        rsite = (round(site.lat, 4), round(site.lon, 4), round(site.alt_m))
        key = (layer, rsite, t0, int(duration_s))
        now = time.monotonic()
        hit = self.cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
        fut = self.inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.pool, _child_chunk, layer, rsite, t0, duration_s)
        self.inflight[key] = fut
        try:
            data = await asyncio.shield(fut)
        finally:
            self.inflight.pop(key, None)
        self._evict(now)
        self.cache[key] = (time.monotonic(), data)
        return data

    def _evict(self, now: float) -> None:
        for k in [k for k, (ts, _) in self.cache.items() if now - ts >= CACHE_TTL_S]:
            del self.cache[k]
