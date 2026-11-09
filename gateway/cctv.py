"""T5.7 mount CCTV: the camera feed through the gateway.

The camera's address and login stay on the gateway (URL + username in settings.json, password in
Credential Manager as `cctv_password`); the browser only talks to our routes. `<img>` / hls.js
cannot send X-Mount-Token, so the stream routes take a short-lived `?t=` ticket that
POST /api/mount/cctv/ticket (header auth) hands out. Read-only: only GETs ever go to the camera.

Nothing here puts the camera URL, the login or a ticket into a response, a log line or the audit:
errors are mapped to fixed readable messages, httpx's own request log is silenced, and the uvicorn
access log is redacted (`redact_ticket_logs`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .audit import AuditLog
from .config import Settings
from .security import origin_ok
from .settings import SettingsStore

log = logging.getLogger("gateway.cctv")
# httpx logs every request line (the camera URL) at INFO: never let that reach a log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TICKET_TTL_S = 60.0  # a fresh ticket must be used within this; a used one lives while it is used
TICKET_MAX = 256
MAX_VIEWERS = 2  # concurrent MJPEG viewers (one shared upstream connection)
VIEWER_QUEUE_UNITS = 60  # frames a slow viewer may lag by before it is dropped (~6 s at 10 fps)
MAX_FRAME_BYTES = 8 * 1024 * 1024
OPEN_TIMEOUT_S = 12.0
HLS_TIMEOUT_S = 15.0
HLS_PLAYLIST_MAX = 1024 * 1024
HLS_SEGMENT_MAX = 16 * 1024 * 1024
SNAPSHOT_MAX = 4 * 1024 * 1024
STATE_TTL_S = 30.0  # how long the last probe / stream result stands without a viewer
HLS_VIEWER_WINDOW_S = 15.0
HLS_ENTRY = "entry.m3u8"  # the configured playlist, under our proxy
PROXY_PREFIX = "/api/mount/cctv/hls"

MSG_UNREACHABLE = "Camera unreachable"
MSG_TIMEOUT = "Camera timed out"
MSG_LOGIN = "Camera rejected the login"
MSG_FORMAT = "Stream format not recognised"
MSG_REDIRECT = "Camera redirected the request (not followed)"
MSG_TOO_LARGE = "Camera response too large"

_HLS_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".vtt": "text/vtt",
}
_URI_ATTR = re.compile(r'URI="([^"]*)"')
_TICKET_IN_LOG = re.compile(r"([?&]t=)[^&\s\"]+")


class CameraError(Exception):
    """A readable camera failure. The message is fixed text: never the URL, login or ticket."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def map_http_error(exc: BaseException) -> CameraError:
    if isinstance(exc, CameraError):
        return exc
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return CameraError(MSG_TIMEOUT)
    return CameraError(MSG_UNREACHABLE)  # refused / DNS / TLS / reset: never echo the detail


def check_status(r: httpx.Response) -> None:
    code = r.status_code
    if code in (401, 403):
        raise CameraError(MSG_LOGIN)
    if 300 <= code < 400:
        raise CameraError(MSG_REDIRECT)
    if code >= 400:
        raise CameraError(f"Camera answered HTTP {code}")


# ---------------------------------------------------------------- upstream auth


class CameraAuth(httpx.Auth):
    """Basic or Digest, whichever the camera asks for, from the stored login (server-side only).

    The first request goes out without credentials; the 401 challenge decides the scheme (so a
    Digest camera never sees the password in a Basic header). Later requests are preemptive.
    """

    def __init__(self, username: str, password: str) -> None:
        self.mode: str | None = None
        self._basic = httpx.BasicAuth(username, password)
        self._digest = httpx.DigestAuth(username, password)

    def auth_flow(self, request: httpx.Request):
        if self.mode == "digest":
            yield from self._digest.auth_flow(request)
            return
        if self.mode == "basic":
            yield from self._basic.auth_flow(request)
            return
        response = yield request
        if response.status_code != 401:
            return
        www = response.headers.get("www-authenticate", "").lower()
        if "digest" in www:
            self.mode = "digest"
            flow = self._digest.auth_flow(request)
            next(flow)  # the plain request: that is the 401 we already hold
            yield flow.send(response)
        elif "basic" in www:
            self.mode = "basic"
            yield from self._basic.auth_flow(request)


# ---------------------------------------------------------------- tickets


class TicketBook:
    def __init__(self, session: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.session = session
        self.clock = clock
        self._t: dict[str, list] = {}  # ticket -> [expires, session]

    def issue(self) -> str:
        self._purge()
        while len(self._t) >= TICKET_MAX:
            self._t.pop(next(iter(self._t)))
        ticket = secrets.token_urlsafe(24)
        self._t[ticket] = [self.clock() + TICKET_TTL_S, self.session]
        return ticket

    def check(self, ticket: str | None, session: str | None = None) -> bool:
        """Valid while unexpired and for this session. Each use renews it (a stream's lifetime)."""
        if not ticket:
            return False
        e = self._t.get(ticket)
        now = self.clock()
        if e is None or now > e[0] or e[1] != (session or self.session):
            self._t.pop(ticket, None)
            return False
        e[0] = now + TICKET_TTL_S
        return True

    def _purge(self) -> None:
        now = self.clock()
        for k in [k for k, e in self._t.items() if now > e[0]]:
            del self._t[k]


def redact_ticket_logs() -> None:
    """Keep `?t=<ticket>` out of the uvicorn access log (idempotent)."""
    lg = logging.getLogger("uvicorn.access")
    if any(isinstance(f, _TicketFilter) for f in lg.filters):
        return
    lg.addFilter(_TicketFilter())


class _TicketFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TICKET_IN_LOG.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
            )
        return True


# ---------------------------------------------------------------- MJPEG


class UnitParser:
    """Splits a multipart stream into units `--boundary ... body` (each kept whole)."""

    def __init__(self, boundary: bytes) -> None:
        self.delim = b"--" + boundary
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        n = len(self.delim)
        i = self.buf.find(self.delim)
        if i < 0:
            # preamble only: keep just enough tail to catch a delimiter split across chunks
            del self.buf[: max(0, len(self.buf) - n)]
            return out
        del self.buf[:i]
        while True:
            j = self.buf.find(self.delim, n)
            if j < 0:
                break
            unit = bytes(self.buf[:j])
            del self.buf[:j]
            if not unit.startswith(self.delim + b"--"):  # the closing `--boundary--`
                out.append(unit)
        if len(self.buf) > MAX_FRAME_BYTES:
            raise CameraError(MSG_FORMAT)
        return out


def boundary_of(content_type: str) -> bytes | None:
    if not content_type.lower().startswith("multipart/x-mixed-replace"):
        return None
    m = re.search(r'boundary\s*=\s*("([^"]+)"|[^;\s]+)', content_type, re.I)
    if not m:
        return None
    b = m.group(2) or m.group(1)
    return b.encode("latin-1", "ignore") or None


def jpeg_of(unit: bytes) -> bytes:
    """The JPEG inside a multipart unit."""
    head, sep, body = unit.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = unit.partition(b"\n\n")
    m = re.search(rb"content-length:\s*(\d+)", head, re.I)
    if m:
        body = body[: int(m.group(1))]
    else:
        body = body.rstrip(b"\r\n")
    if not body.startswith(b"\xff\xd8"):
        raise CameraError(MSG_FORMAT)
    return body


class Viewer:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(VIEWER_QUEUE_UNITS)
        self.dropped = False


class MjpegHub:
    """One upstream connection, fanned out to up to MAX_VIEWERS; closed with the last viewer."""

    def __init__(self, svc: CctvService) -> None:
        self.svc = svc
        self.viewers: set[Viewer] = set()
        self.task: asyncio.Task | None = None
        self.opened: asyncio.Future[str] | None = None
        self.connected = False
        self.last_unit: bytes | None = None

    def join(self) -> Viewer | None:
        if len(self.viewers) >= MAX_VIEWERS:
            return None
        v = Viewer()
        self.viewers.add(v)
        if self.task is None or self.task.done():
            self.opened = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(self._run(self.opened))
        elif self.last_unit is not None:
            v.queue.put_nowait(self.last_unit)  # the picture now, not after the next frame
        return v

    def leave(self, v: Viewer) -> None:
        self.viewers.discard(v)
        if not self.viewers and self.task is not None:
            self.task.cancel()  # closes the upstream request (async with in _run)
            self.task = None
            self.connected = False
            self.last_unit = None

    async def wait_open(self) -> str:
        assert self.opened is not None
        return await asyncio.wait_for(asyncio.shield(self.opened), OPEN_TIMEOUT_S)

    async def stream(self, v: Viewer) -> AsyncIterator[bytes]:
        try:
            while True:
                unit = await v.queue.get()
                if unit is None:
                    return
                yield unit
        finally:
            self.leave(v)

    def _fan_out(self, unit: bytes) -> None:
        self.last_unit = unit
        for v in list(self.viewers):
            try:
                v.queue.put_nowait(unit)
            except asyncio.QueueFull:  # stopped reading: drop it, do not stall the others
                v.dropped = True
                self.viewers.discard(v)
                while not v.queue.empty():
                    v.queue.get_nowait()
                v.queue.put_nowait(None)

    async def _run(self, opened: asyncio.Future[str]) -> None:
        error: CameraError | None = None
        try:
            client = self.svc.client()
            async with client.stream("GET", self.svc.cfg.url) as r:
                check_status(r)
                ct = r.headers.get("content-type", "")
                boundary = boundary_of(ct)
                if boundary is None:
                    raise CameraError(MSG_FORMAT)
                parser = UnitParser(boundary)
                self.connected = True
                self.svc.record(None)
                if not opened.done():
                    opened.set_result(ct)
                async for chunk in r.aiter_bytes():
                    for unit in parser.feed(chunk):
                        self._fan_out(unit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = map_http_error(exc)
        finally:
            if self.task is asyncio.current_task():  # not already replaced by a newer run
                self.connected = False
                self.last_unit = None
        if error is None:
            error = CameraError("Camera stream ended")
        self.svc.record(error.message)
        if not opened.done():
            opened.set_exception(error)
            opened.exception()  # retrieved: nobody may be waiting any more
        for v in list(self.viewers):
            with contextlib.suppress(asyncio.QueueFull):
                v.queue.put_nowait(None)

    async def aclose(self) -> None:
        for v in list(self.viewers):
            v.dropped = True
            with contextlib.suppress(asyncio.QueueFull):
                v.queue.put_nowait(None)
        self.viewers.clear()
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------- HLS


def base_of(url: str) -> tuple[str, str]:
    """(origin, base path ending in /) of the configured playlist URL."""
    u = urlsplit(url)
    path = u.path or "/"
    return f"{u.scheme}://{u.netloc}", path[: path.rfind("/") + 1]


def _proxy_uri(rel: str, query: str, ticket: str) -> str:
    out = f"{PROXY_PREFIX}/{quote(rel)}?t={ticket}"
    return out + (f"&q={quote(query, safe='')}" if query else "")


def rewrite_playlist(text: str, playlist_url: str, config_url: str, ticket: str) -> str:
    """Point every URI of an HLS playlist at our proxy. A URI outside the configured playlist's
    base path (or on another host) is replaced by a path the proxy refuses: no open proxy."""
    origin, base = base_of(config_url)

    def one(uri: str) -> str:
        sp = urlsplit(urljoin(playlist_url, uri))
        inside = (
            f"{sp.scheme}://{sp.netloc}" == origin
            and sp.path.startswith(base)
            and ".." not in sp.path.split("/")
        )
        if not inside or sp.path == base:
            return _proxy_uri("_blocked", "", ticket)
        return _proxy_uri(sp.path[len(base) :], sp.query, ticket)

    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            lines.append(line)
        elif s.startswith("#"):
            lines.append(_URI_ATTR.sub(lambda m: f'URI="{one(m.group(1))}"', line))
        else:
            lines.append(one(s))
    return "\n".join(lines) + "\n"


def hls_path_ok(path: str) -> bool:
    """A proxy path is relative, plain and traversal-free."""
    if not path or path.startswith("/") or "\\" in path or "?" in path or "#" in path:
        return False
    if any(ord(c) < 32 or c == "\x7f" for c in path):
        return False
    return all(seg not in ("", ".", "..") for p in (path, unquote(path)) for seg in p.split("/"))


# ---------------------------------------------------------------- service


class CctvService:
    def __init__(
        self,
        store: SettingsStore,
        audit: AuditLog,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.audit = audit
        self.transport = transport
        self.clock = clock
        self.tickets = TicketBook(hashlib.sha256(token.encode()).hexdigest()[:16], clock)
        self.hub = MjpegHub(self)
        self._client: httpx.AsyncClient | None = None
        self._last: tuple[bool, str | None, float] | None = None  # (ok, error, at)
        self._announced: tuple[str, str | None] | None = None
        self._hls_seen: dict[str, float] = {}
        self._probe_task: asyncio.Task | None = None
        self._snap_lock = asyncio.Lock()

    @property
    def cfg(self):
        return self.store.current.cctv

    # -- state ------------------------------------------------------------

    def record(self, error: str | None) -> None:
        """A stream / probe / fetch result: None = the camera answered fine."""
        self._last = (error is None, error, self.clock())
        self._announce()

    def _announce(self) -> None:
        cur = (self.state(), self._last[1] if self._last else None)
        if cur != self._announced:
            self._announced = cur
            self.audit.write("cctv_state", result=cur[0], reason=cur[1])
            log.info("cctv %s%s", cur[0], f" ({cur[1]})" if cur[1] else "")

    def viewers(self) -> int:
        now = self.clock()
        self._hls_seen = {k: v for k, v in self._hls_seen.items() if now - v < HLS_VIEWER_WINDOW_S}
        return len(self.hub.viewers) + len(self._hls_seen)

    def state(self) -> str:
        if not self.cfg.configured:
            return "not_configured"
        if self.hub.connected:
            return "live"
        if self._last is not None and self.clock() - self._last[2] < STATE_TTL_S:
            return "live" if self._last[0] else "unreachable"
        return "connecting"

    def status(self) -> dict:
        state = self.state()
        if state == "connecting" and self.viewers() == 0:
            self._kick_probe()
        err = self._last[1] if self._last and state == "unreachable" else None
        return {
            "configured": self.cfg.configured,
            "type": self.cfg.type,
            "state": state,
            "error": err,
            "viewers": self.viewers(),
        }

    def status_line(self) -> str:
        """The Settings dialog's status text."""
        state = self.state()
        if state == "not_configured":
            return "Not configured"
        if state == "unreachable":
            return f"Unreachable: {self._last[1] if self._last else MSG_UNREACHABLE}"
        return "Live" if state == "live" else "Connecting"

    async def config_changed(self) -> None:
        """Settings were saved: forget the old result, the old client and any running stream."""
        self._last = None
        self._announced = None
        await self.hub.aclose()
        await self._drop_client()

    # -- upstream ---------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            user = self.cfg.username
            try:
                password = self.store.cctv_secrets.get() or ""
            except Exception:
                password = ""
            self._client = httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,  # a redirect would be an open-proxy hop
                trust_env=False,  # never through a system proxy (the camera is on the LAN)
                auth=CameraAuth(user, password) if user else None,
                headers={"User-Agent": "pw-mount-control-cctv"},
            )
        return self._client

    async def _drop_client(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            with contextlib.suppress(Exception):
                await c.aclose()

    async def aclose(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            await asyncio.gather(self._probe_task, return_exceptions=True)
        await self.hub.aclose()
        await self._drop_client()

    def _kick_probe(self) -> None:
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe())

    async def _probe(self) -> None:
        """Open the camera once to learn whether it answers (headers / playlist head only)."""
        try:
            async with asyncio.timeout(OPEN_TIMEOUT_S):
                async with self.client().stream("GET", self.cfg.url) as r:
                    check_status(r)
                    ct = r.headers.get("content-type", "")
                    if self.cfg.type == "mjpeg":
                        if boundary_of(ct) is None and not ct.lower().startswith("image/jpeg"):
                            raise CameraError(MSG_FORMAT)
                    else:
                        head = b""
                        async for chunk in r.aiter_bytes():
                            head += chunk
                            if len(head) >= 16:
                                break
                        if not head.lstrip(b"\xef\xbb\xbf").startswith(b"#EXTM3U"):
                            raise CameraError(MSG_FORMAT)
            self.record(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.record(map_http_error(exc).message)

    # -- snapshot ---------------------------------------------------------

    async def snapshot(self) -> bytes:
        if self.hub.last_unit is not None:
            with contextlib.suppress(CameraError):
                return jpeg_of(self.hub.last_unit)
        async with self._snap_lock:
            try:
                async with asyncio.timeout(OPEN_TIMEOUT_S):
                    async with self.client().stream("GET", self.cfg.url) as r:
                        check_status(r)
                        ct = r.headers.get("content-type", "")
                        boundary = boundary_of(ct)
                        if boundary is None and ct.lower().startswith("image/jpeg"):
                            buf = bytearray()
                            async for chunk in r.aiter_bytes():
                                buf += chunk
                                if len(buf) > SNAPSHOT_MAX:
                                    raise CameraError(MSG_TOO_LARGE)
                            frame = bytes(buf)
                            if not frame.startswith(b"\xff\xd8"):
                                raise CameraError(MSG_FORMAT)
                        elif boundary is not None:
                            parser = UnitParser(boundary)
                            frame = b""
                            async for chunk in r.aiter_bytes():
                                units = parser.feed(chunk)
                                if units:
                                    frame = jpeg_of(units[0])
                                    break
                            if not frame:
                                raise CameraError(MSG_FORMAT)
                        else:
                            raise CameraError(MSG_FORMAT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = map_http_error(exc)
                self.record(err.message)
                raise err from None
        self.record(None)
        return frame

    # -- HLS --------------------------------------------------------------

    async def hls(self, path: str, query: str, ticket: str) -> Response:
        cfg_url = self.cfg.url
        origin, base = base_of(cfg_url)
        self._hls_seen[hashlib.sha256(ticket.encode()).hexdigest()[:12]] = self.clock()
        if path == HLS_ENTRY:
            upstream, playlist_url = cfg_url, cfg_url
        else:
            if path == "_blocked" or not hls_path_ok(path):
                raise HTTPException(status_code=404, detail="not found")
            rel = path
            merged = parse_qsl(urlsplit(cfg_url).query) + parse_qsl(query)
            upstream = origin + base + quote(rel, safe="/@:+,=!$&'()*;~-._")
            playlist_url = upstream
            if merged:
                upstream += "?" + urlencode(merged)
        is_playlist = path == HLS_ENTRY or urlsplit(path).path.lower().endswith(".m3u8")
        cap = HLS_PLAYLIST_MAX if is_playlist else HLS_SEGMENT_MAX
        try:
            async with asyncio.timeout(HLS_TIMEOUT_S):
                async with self.client().stream("GET", upstream) as r:
                    check_status(r)
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > cap:
                            raise CameraError(MSG_TOO_LARGE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = map_http_error(exc)
            self.record(err.message)
            raise HTTPException(status_code=502, detail=err.message) from None
        headers = {"Cache-Control": "no-store", "Content-Security-Policy": "sandbox"}
        if is_playlist:
            text = bytes(buf).decode("utf-8", "replace").lstrip("﻿")
            if not text.startswith("#EXTM3U"):
                self.record(MSG_FORMAT)
                raise HTTPException(status_code=502, detail=MSG_FORMAT)
            self.record(None)
            body = rewrite_playlist(text, playlist_url, cfg_url, ticket).encode()
            return Response(body, media_type=_HLS_TYPES[".m3u8"], headers=headers)
        self.record(None)
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return Response(
            bytes(buf), media_type=_HLS_TYPES.get(ext, "application/octet-stream"), headers=headers
        )


# ---------------------------------------------------------------- routes


def build_router(
    svc: CctvService, settings: Settings, require_auth: Callable[[Request], None]
) -> APIRouter:
    router = APIRouter(prefix="/api/mount/cctv")

    def stream_guard(request: Request, t: str | None = None) -> str:
        """Stream routes: a valid ticket + the same Origin / Sec-Fetch rules as require_auth."""
        h = request.headers
        origin = h.get("origin")
        if origin is not None:
            if not origin_ok(settings, origin):
                raise HTTPException(status_code=403, detail="forbidden origin")
        elif h.get("sec-fetch-site") not in (None, "same-origin", "none"):
            raise HTTPException(status_code=403, detail="forbidden")
        if not svc.tickets.check(t):
            raise HTTPException(status_code=401, detail="bad or expired ticket")
        if not svc.cfg.configured:
            raise HTTPException(status_code=404, detail="CCTV not configured")
        assert t is not None
        return t

    def expect(kind: str) -> None:
        if svc.cfg.type != kind:
            raise HTTPException(status_code=409, detail=f"CCTV is set to {svc.cfg.type.upper()}")

    no_store = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    @router.get("/status", dependencies=[Depends(require_auth)])
    def status() -> JSONResponse:
        return JSONResponse(svc.status(), headers=no_store)

    @router.post("/ticket", dependencies=[Depends(require_auth)])
    def ticket() -> JSONResponse:
        if not svc.cfg.configured:
            raise HTTPException(status_code=409, detail="CCTV not configured")
        return JSONResponse(
            {"ticket": svc.tickets.issue(), "expires_in": TICKET_TTL_S}, headers=no_store
        )

    @router.get("/mjpeg")
    async def mjpeg(t: str = Depends(stream_guard)) -> Response:
        expect("mjpeg")
        hub = svc.hub
        v = hub.join()
        if v is None:
            raise HTTPException(status_code=429, detail="Too many CCTV viewers")
        try:
            ct = await hub.wait_open()
        except BaseException as exc:
            hub.leave(v)
            if isinstance(exc, asyncio.CancelledError):
                raise
            err = map_http_error(exc)
            raise HTTPException(status_code=502, detail=err.message) from None
        return StreamingResponse(
            hub.stream(v),
            media_type=ct,
            headers={**no_store, "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/snapshot")
    async def snapshot(t: str = Depends(stream_guard)) -> Response:
        expect("mjpeg")
        try:
            frame = await svc.snapshot()
        except CameraError as err:
            raise HTTPException(status_code=502, detail=err.message) from None
        return Response(
            frame,
            media_type="image/jpeg",
            headers={**no_store, "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/hls/{path:path}")
    async def hls(path: str, request: Request, t: str = Depends(stream_guard)) -> Response:
        expect("hls")
        return await svc.hls(path, request.query_params.get("q", ""), t)

    return router


def install(
    app: Any,
    store: SettingsStore,
    audit: AuditLog,
    settings: Settings,
    token: str,
    require_auth: Callable[[Request], None],
    transport: httpx.AsyncBaseTransport | None = None,
) -> CctvService:
    redact_ticket_logs()
    svc = CctvService(store, audit, token, transport)
    app.include_router(build_router(svc, settings, require_auth))
    app.state.cctv = svc
    return svc
