from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse

from .audit import AuditLog
from .config import Settings
from .control import ControlService, Pwi4Writer
from .ephem import Ephem
from .hub import Hub
from .poller import Poller
from .safety import SafetyEngine, SafetyMonitor
from .security import (
    HostCheckMiddleware,
    new_token,
    origin_ok,
    session_request_ok,
    token_ok,
)
from .settings import Location, SecretStore, SettingsStore, SettingsUpdate, compute_site_check
from .snapshot import site_key
from .transforms import AltAzValues, TransformRequest

VERSION = "0.1.0"
AUTH_TIMEOUT_S = 2.0
WS_UNAUTHORIZED = 4401


def with_control(msg: str, control: dict) -> str:
    """Add the per-client `control` field to a serialized status envelope's `data` object."""
    if msg.endswith("}}"):
        return msg[:-2] + ',"control":' + json.dumps(control) + "}}"
    return msg


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    secrets: SecretStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    token = settings.token or new_token()
    hub = Hub()
    ephem = Ephem(settings.ephemeris)
    store = SettingsStore(settings.settings_path, secrets)
    poller = Poller(settings, hub, ephem, transport, store)
    audit = AuditLog(settings.audit_dir)
    control = ControlService(Pwi4Writer(settings.pwi4_url, transport), poller, hub, audit)
    safety = SafetyEngine.from_env(poller, store, ephem, audit)
    control.check = safety.check  # T3.1b: command gate
    control.resolved_az = safety.pop_target_az
    monitor = SafetyMonitor(safety, poller, control)
    control.motion_cleared = monitor.clear
    control.home_check = safety.check_home  # T3.2: HOME gate (Sun path, site, Lim EL)
    control.homing_begin = monitor.begin_homing
    control.homing_end = monitor.end_homing
    control.seq.exe = settings.pwi4_exe
    control.seq.start_timeout = settings.pwi4_start_timeout_s
    poller.sequence_provider = control.seq.snapshot_info
    poller.homed_observer = control.homed.observe  # T3.3 not-homed guard
    poller.homed_provider = control.homed.info

    def goto_started(cmd) -> None:
        plan = safety.take_plan(cmd)
        if plan is not None:
            monitor.begin(plan)

    control.goto_started = goto_started

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.poll_enabled and store.first_run:
            # First run: seed the location from PWI4's site if it answers, else the defaults.
            with contextlib.suppress(Exception):
                await poller.fetch_once()
            with contextlib.suppress(Exception):
                store.seed_from_pwi4(site_key(poller.last_good) if poller.last_good else None)
        audit.cleanup()
        task = asyncio.create_task(poller.run()) if settings.poll_enabled else None
        # G4 start-up stop: before any command is accepted (the WS only opens after this).
        control.start()
        await control.startup()
        monitor.start()
        try:
            yield
        finally:
            await monitor.stop()
            await control.shutdown()  # G4 graceful-exit stop
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="pw-mount-control gateway", version=VERSION, lifespan=lifespan)
    app.add_middleware(HostCheckMiddleware, settings=settings)  # no CORS middleware, on purpose
    app.state.settings = settings
    app.state.hub = hub
    app.state.poller = poller
    app.state.ephem = ephem
    app.state.store = store
    app.state.control = control
    app.state.safety = safety
    app.state.monitor = monitor
    app.state.audit = audit

    def require_auth(request: Request) -> None:
        """REST guard: per-launch token header + Origin / Sec-Fetch-Site (Host: middleware)."""
        h = request.headers
        if not token_ok(token, h.get("x-mount-token")):
            raise HTTPException(status_code=401, detail="missing or bad token")
        origin = h.get("origin")
        if origin is not None:
            if not origin_ok(settings, origin):
                raise HTTPException(status_code=403, detail="forbidden origin")
        elif h.get("sec-fetch-site") not in (None, "same-origin", "none"):
            raise HTTPException(status_code=403, detail="forbidden")

    @app.get("/api/mount/health")
    def health() -> dict:
        return {"ok": True, "service": "gateway", "version": VERSION}

    @app.get("/api/mount/status")
    def status() -> Response:
        snap = poller.latest
        if snap is None:
            return JSONResponse({"detail": "no snapshot yet"}, status_code=503)
        return Response(snap.model_dump_json(), media_type="application/json")

    @app.get("/api/mount/session")
    def session(request: Request) -> Response:
        h = request.headers
        if not session_request_ok(settings, h.get("sec-fetch-site"), h.get("origin")):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})

    def with_check(body: dict) -> dict:
        body["site_check"] = poller.current_site_check().model_dump()
        return body

    @app.get("/api/mount/settings", dependencies=[Depends(require_auth)])
    def get_settings() -> JSONResponse:
        return JSONResponse(store.public(), headers={"Cache-Control": "no-store"})

    @app.put("/api/mount/settings", dependencies=[Depends(require_auth)])
    async def put_settings(update: SettingsUpdate) -> JSONResponse:
        try:
            store.save(update, update.api_key)
        except Exception as exc:  # keyring / disk failure: never echo details (may touch a key)
            raise HTTPException(
                status_code=503, detail=f"could not save ({type(exc).__name__})"
            ) from None
        await poller.refresh_ephem(force=True)  # apply live: Sun/Moon for the new site now
        return JSONResponse(with_check(store.public()), headers={"Cache-Control": "no-store"})

    @app.get("/api/mount/site-check", dependencies=[Depends(require_auth)])
    def site_check() -> JSONResponse:
        return JSONResponse(poller.current_site_check().model_dump())

    @app.post("/api/mount/site-check", dependencies=[Depends(require_auth)])
    def site_check_proposed(loc: Location) -> JSONResponse:
        """Check a PROPOSED location against PWI4's cached site. Read-only: nothing is saved."""
        return JSONResponse(compute_site_check(loc, poller.pwi4_site()))

    @app.post("/api/transform", dependencies=[Depends(require_auth)])
    def transform(req: TransformRequest) -> JSONResponse:
        """H4 (T3.3): J2000 RA/Dec <-> apparent topocentric alt/az at an explicit time, no
        refraction. Read-only (no control lock). Default site = PWI4's own site."""
        if not ephem.available:
            raise HTTPException(status_code=503, detail="ephemeris unavailable")
        if req.site is not None:
            site = (req.site.lat, req.site.lon, req.site.height_m)
        else:
            site = poller.pwi4_site()
            if site is None:
                raise HTTPException(status_code=503, detail="PWI4 site unavailable")
        t = req.time()
        v = req.values
        if isinstance(v, AltAzValues):
            ra_h, dec = ephem.altaz_radec_j2000(site, v.alt, v.az, t)
            result = {"ra_h": ra_h, "dec_deg": dec}
        else:
            alt, az = ephem.radec_j2000_altaz(site, v.ra_h, v.dec_deg, t)
            result = {"alt": alt, "az": az}
        body = {
            "dir": req.dir,
            "t_utc": t.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "site": {"lat": site[0], "lon": site[1], "height_m": site[2]},
            "result": result,
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.websocket("/api/mount/ws")
    async def ws(websocket: WebSocket) -> None:
        if not origin_ok(settings, websocket.headers.get("origin")):
            await websocket.close(code=1008)  # before accept -> HTTP 403 handshake refusal
            return
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT_S)
            msg = json.loads(raw)
            ok = (
                isinstance(msg, dict)
                and msg.get("type") == "auth"
                and token_ok(token, msg.get("token"))
            )
        except Exception:
            ok = False
        if not ok:
            with contextlib.suppress(Exception):
                await websocket.close(code=WS_UNAUTHORIZED)
            return

        hello = {
            "type": "hello",
            "seq": 0,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "data": {"service": "gateway", "version": VERSION, "poll_hz": settings.poll_hz},
        }
        client = hub.add()
        send_lock = asyncio.Lock()  # status stream and acks share the socket
        cmd_tasks: set[asyncio.Task] = set()

        async def send(text: str) -> None:
            async with send_lock:
                await websocket.send_text(text)

        async def sender() -> None:
            await send(json.dumps(hello))
            while True:
                msg = await client.queue.get()
                await send(with_control(msg, control.control_for(client.id)))

        async def run_command(raw: str) -> None:
            try:
                ack = await control.handle(client.id, raw)
                await send(json.dumps(ack))
            except Exception:
                pass  # socket gone; the command (if any) was already audited

        async def receiver() -> None:
            while True:
                raw = await websocket.receive_text()
                t = asyncio.create_task(run_command(raw))  # never block the next frame (STOP)
                cmd_tasks.add(t)
                t.add_done_callback(cmd_tasks.discard)

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            hub.remove(client)  # fires the link-loss stop when this was the control holder
            # Command tasks are left to finish: a STOP in flight must not be cancelled here.
            with contextlib.suppress(Exception):
                await websocket.close()

    return app


app = create_app()
