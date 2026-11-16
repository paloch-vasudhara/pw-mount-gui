"""T6.2a one origin: the gateway serves the built web app and proxies the analysis API.

Two last-resort routes, registered after every gateway route (so each `/api/mount/*` route, the
WebSocket and `/api/transform` keep priority):

- `/api/{path}`: any other `/api/*` request is streamed to the analysis service (same path and
  query, any method) and its answer streamed back. The browser-side guards of the gateway's own
  HTTP API apply (Host check is global; a cross-site Origin / Sec-Fetch-Site is refused), and
  nothing that identifies the session (token, cookie, authorization, origin) goes upstream.
- `/{path}`: GET / HEAD static files of the generated web app (`pnpm generate`), SPA fallback for
  extension-less unknown paths. Without a web dir there is no static serving (dev mode).
"""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Match

from .config import Settings
from .security import origin_ok

ANALYSIS_DOWN = "Analysis service unavailable"
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 120.0
UPLOAD_TIMEOUT_S = 600.0
MAX_BODY_BYTES = 200 * 1024 * 1024 + 1024 * 1024  # analysis MAX_UPLOAD_BYTES + multipart overhead
WEB_HINT = "web app not built: run pnpm generate, or use the dev server on 3200"

# Never forwarded upstream (everything not in FORWARD_REQUEST is dropped anyway; this documents it).
FORWARD_REQUEST = ("content-type", "content-length", "accept", "accept-encoding", "range")
HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer", "trailers"}
)
# also dropped from the answer: uvicorn adds its own
DROP_RESPONSE = HOP_BY_HOP | {"server", "date"}
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Set explicitly: Windows' registry mimetypes can be wrong (.js as text/plain).
CONTENT_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".bin": "application/octet-stream",
}
IMMUTABLE = "public, max-age=31536000, immutable"
HTML_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class _TooLarge(Exception):
    pass


# ---------------------------------------------------------------- static


def _safe_rel(path: str) -> str | None:
    """The request path as a plain relative path, or None when it is anything else."""
    rel = path.lstrip("/")
    if "\\" in rel or ":" in rel or "\x00" in rel:
        return None
    if any(seg in ("..", ".") for seg in rel.split("/")):
        return None
    return rel


def _file_response(file: Path, rel: str) -> FileResponse:
    ext = file.suffix.lower()
    ctype = CONTENT_TYPES.get(ext) or mimetypes.guess_type(file.name)[0]
    headers = {"X-Content-Type-Options": "nosniff"}
    if ext == ".html":
        headers.update(HTML_HEADERS)
        headers["Cache-Control"] = "no-store"
    elif rel.startswith("_nuxt/") and not rel.startswith("_nuxt/builds/"):
        headers["Cache-Control"] = IMMUTABLE
    else:
        headers["Cache-Control"] = "no-cache"
    return FileResponse(file, media_type=ctype or "application/octet-stream", headers=headers)


def _install_static(app: FastAPI, web_dir: Path | None) -> None:
    if web_dir is None or not web_dir.is_dir():

        @app.get("/")
        def no_web() -> JSONResponse:
            return JSONResponse({"detail": WEB_HINT}, status_code=404)

        return

    root = web_dir.resolve()

    def pick(rel: str) -> Path | None:
        target = (root / rel).resolve() if rel else root
        if target != root and not target.is_relative_to(root):
            return None
        if target.is_dir():  # a prerendered route folder; never a listing
            target = target / "index.html"
        return target if target.is_file() and target.resolve().is_relative_to(root) else None

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def static(path: str) -> Response:
        rel = _safe_rel(path)
        if rel is None or path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        file = pick(rel)
        if file is not None:
            return _file_response(file, rel)
        if "." in rel.rsplit("/", 1)[-1]:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # SPA fallback (ssr: false): Nuxt's 200.html, else index.html
        for name in ("200.html", "index.html"):
            if (root / name).is_file():
                return _file_response(root / name, name)
        return JSONResponse({"detail": "Not Found"}, status_code=404)


# ---------------------------------------------------------------- analysis proxy


class AnalysisProxy:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> None:
        self.base = settings.analysis_url
        self.settings = settings
        self.transport = transport
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                follow_redirects=False,
                trust_env=False,  # never through a system proxy (loopback)
            )
        return self._client

    async def aclose(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            try:
                await c.aclose()
            except Exception:
                pass

    def guard(self, request: Request) -> Response | None:
        """The same browser-side rules as the gateway's own API (no token: analysis has none)."""
        h = request.headers
        origin = h.get("origin")
        bad = origin is not None and not origin_ok(self.settings, origin)
        if bad or h.get("sec-fetch-site") in ("cross-site", "same-site"):
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        return None

    def redirect_target(self, location: str) -> str | None:
        """A redirect that stays in our /api path space, as a relative reference; else None."""
        sp = urlsplit(location)
        if sp.scheme or sp.netloc:
            up = urlsplit(self.base)
            if (sp.scheme, sp.netloc) != (up.scheme, up.netloc):
                return None
        if not sp.path.startswith("/api/") or ".." in sp.path.split("/"):
            return None
        return sp.path + (f"?{sp.query}" if sp.query else "")

    async def forward(self, request: Request) -> Response:
        denied = self.guard(request)
        if denied is not None:
            return denied
        path = request.scope.get("raw_path")
        path = path.decode("latin-1") if path else request.url.path
        path = path.split("?", 1)[0]
        decoded = unquote(path)
        if (
            path == "/api/mount"
            or path.startswith("/api/mount/")
            or path == "/api/transform"
            or "\\" in decoded
            or ".." in decoded.split("/")
        ):
            # the gateway's own space: never relayed; a wrong method on a real route stays a 405
            if any(r.matches(request.scope)[0] == Match.PARTIAL for r in request.app.routes):
                return JSONResponse({"detail": "Method Not Allowed"}, status_code=405)
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        headers = {k: v for k in FORWARD_REQUEST if (v := request.headers.get(k)) is not None}
        has_body = (
            request.method in BODY_METHODS
            or "content-length" in request.headers
            or "transfer-encoding" in request.headers
        )
        limit = MAX_BODY_BYTES
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                return JSONResponse({"detail": "bad Content-Length"}, status_code=400)
            if too_big:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        if "transfer-encoding" in request.headers:
            headers.pop("content-length", None)

        async def body() -> AsyncIterator[bytes]:
            n = 0
            async for chunk in request.stream():
                n += len(chunk)
                if n > limit:
                    raise _TooLarge
                yield chunk

        url = self.base + path
        if request.url.query:
            url += "?" + request.url.query
        timeout = httpx.Timeout(
            UPLOAD_TIMEOUT_S if has_body else READ_TIMEOUT_S,
            connect=CONNECT_TIMEOUT_S,
        )
        try:
            req = self.client().build_request(
                request.method,
                url,
                headers=headers,
                content=body() if has_body else None,
                timeout=timeout,
            )
            async with asyncio.timeout(UPLOAD_TIMEOUT_S if has_body else None):
                upstream = await self.client().send(req, stream=True)
        except _TooLarge:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        except Exception as exc:  # httpx errors can embed the URL: never echo them
            if isinstance(exc.__cause__, _TooLarge):
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
            return JSONResponse({"detail": ANALYSIS_DOWN}, status_code=503)

        out = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE}
        if 300 <= upstream.status_code < 400:
            loc = upstream.headers.get("location")
            target = self.redirect_target(loc) if loc else None
            await upstream.aclose()
            if loc and target is None:
                return JSONResponse(
                    {"detail": "Analysis service sent an unusable redirect"}, status_code=502
                )
            if target is not None:
                out = {k: v for k, v in out.items() if k.lower() != "location"}
                out["location"] = target
            return Response(status_code=upstream.status_code, headers=out)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except Exception:
                return  # the upstream broke mid-answer: end the stream
            finally:
                await upstream.aclose()

        return StreamingResponse(stream(), status_code=upstream.status_code, headers=out)


def install(
    app: FastAPI,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AnalysisProxy:
    """Register LAST: both routes are catch-alls under everything the gateway serves itself."""
    proxy = AnalysisProxy(settings, transport)

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def analysis(request: Request, path: str) -> Response:
        return await proxy.forward(request)

    _install_static(app, settings.web_dir)
    app.state.analysis_proxy = proxy
    return proxy
