from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .catalog import LAYERS, default_source
from .chunks import MAX_DURATION_S, STEP_S, Site
from .db import CatalogUnavailable
from .service import Service, Unavailable
from .state import compute_state

VERSION = "0.1.0"
log = logging.getLogger("analysis")


class SiteIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-360, le=360)
    alt_m: float = Field(default=0.0, ge=-1000, le=20000)


class ChunkIn(BaseModel):
    layer: Literal["leo", "meo", "geo"]
    site: SiteIn
    t0_unix: float
    duration_s: float = Field(default=1800, gt=0, le=MAX_DURATION_S)


class StateIn(BaseModel):
    norad: int
    site: SiteIn
    t_unix: float | None = None


def create_app(factory: Callable = default_source) -> FastAPI:
    service = Service(factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="pw-mount-control analysis", version=VERSION, lifespan=lifespan)
    app.state.service = service

    def ready() -> None:
        try:
            service.require_ready()
        except Unavailable as e:
            raise HTTPException(503, str(e)) from None

    def db_call(fn):
        """Run a read-only catalogue query; never leak exception text (it can embed the DSN)."""
        try:
            return fn()
        except CatalogUnavailable as e:
            raise HTTPException(503, str(e)) from None
        except Exception:
            log.warning("catalogue query failed")
            raise HTTPException(503, "satellite database unavailable") from None

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "service": "analysis", "version": VERSION}

    @app.get("/api/sats/status")
    def sats_status() -> dict:
        return service.status()

    @app.post("/api/sats/refresh")
    async def sats_refresh() -> dict:
        res = await service.refresh()
        if not res["ok"]:
            raise HTTPException(503, "satellite catalogue could not be loaded")
        return res

    @app.get("/api/sats/search")
    def sats_search(
        q: str | None = None,
        orbit_class: str | None = None,
        object_type: str | None = None,
        limit: int = Query(500, ge=1, le=500),
    ) -> list[dict]:
        return db_call(lambda: service.get_source().search(q, orbit_class, object_type, limit))

    @app.get("/api/sats/{norad}")
    def sats_get(norad: int) -> dict:
        row = db_call(lambda: service.get_source().get(norad))
        if row is None:
            raise HTTPException(404, "not found or decayed")
        return row

    @app.post("/api/sky/state")
    def sky_state(body: StateIn) -> dict:
        row = service.tle_row(body.norad, lambda n: db_call(lambda: service.get_source().get(n)))
        if row is None:
            raise HTTPException(404, "not found or decayed")
        t = body.t_unix if body.t_unix is not None else time.time()
        return compute_state(row, body.site.lat, body.site.lon, body.site.alt_m, t)

    @app.post("/api/sky/chunk")
    async def sky_chunk(body: ChunkIn) -> Response:
        try:
            data = await service.chunk(
                body.layer,
                Site(body.site.lat, body.site.lon, body.site.alt_m),
                body.t0_unix,
                body.duration_s,
            )
        except Unavailable as e:
            raise HTTPException(503, str(e)) from None
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/api/sky/objects")
    def sky_objects(layer: Literal["leo", "meo", "geo"]) -> list[list]:
        ready()
        return service.layers[layer].objects()

    return app


assert set(STEP_S) == set(LAYERS)
app = create_app()
