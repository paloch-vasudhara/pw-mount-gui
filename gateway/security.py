"""Localhost hardening (I7): Host check, Origin check, per-launch token."""

from __future__ import annotations

import hmac
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_ok(expected: str, given: object) -> bool:
    return isinstance(given, str) and hmac.compare_digest(expected.encode(), given.encode())


def host_ok(settings: Settings, host: str | None) -> bool:
    return host is not None and host.lower() in settings.allowed_hosts


def origin_ok(settings: Settings, origin: str | None) -> bool:
    """A missing Origin is rejected."""
    return origin is not None and origin.rstrip("/") in settings.allowed_origins


def session_request_ok(settings: Settings, sec_fetch_site: str | None, origin: str | None) -> bool:
    if sec_fetch_site in ("same-origin", "none"):
        return True
    return origin_ok(settings, origin)


class HostCheckMiddleware:
    """DNS-rebinding guard: bad Host -> HTTP 400 (WebSocket: handshake refused)."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = None
            for k, v in scope.get("headers", []):
                if k == b"host":
                    host = v.decode("latin-1")
                    break
            if not host_ok(self.settings, host):
                if scope["type"] == "http":
                    body = b'{"detail":"bad Host header"}'
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 400,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                else:
                    await receive()  # websocket.connect
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)
