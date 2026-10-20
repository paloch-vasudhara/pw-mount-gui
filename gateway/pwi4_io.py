"""Async PWI4 status fetch. READ-ONLY: this module only ever GETs /status."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import REPO_ROOT

_PWI4_DIR = str(REPO_ROOT / "pwi4")
if _PWI4_DIR not in sys.path:
    sys.path.insert(0, _PWI4_DIR)

from pwi4_client import PWI4  # noqa: E402  (vendor module, unmodified)

TIMEOUT_S = 0.5  # read timeout
CONNECT_TIMEOUT_S = 0.25  # closed localhost ports take the full timeout to fail on Windows
TOTAL_TIMEOUT_S = 0.8  # hard cap on one /status fetch
_parser = PWI4()  # used for parse_status only; never issues a request


@dataclass
class PollResult:
    reachable: bool
    status: Any = None  # pwi4_client.PWI4Status when reachable
    error: str | None = None
    latency_ms: float | None = None
    kind: str = "ok"  # ok | unreachable (refused / connect timeout) | error (anything else)


def parse(text: str | bytes) -> Any:
    return _parser.parse_status(text)


def new_client(
    base_url: str, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        transport=transport,
    )


async def fetch_status(client: httpx.AsyncClient) -> PollResult:
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(client.get("/status"), TOTAL_TIMEOUT_S)
        latency = (time.perf_counter() - t0) * 1000.0
        if resp.status_code != 200:
            return PollResult(
                False, error=f"HTTP {resp.status_code}", latency_ms=latency, kind="error"
            )
        try:
            status = parse(resp.content)
        except Exception as exc:  # malformed / unexpected status text
            return PollResult(
                False, error=f"unparseable status: {exc!r}", latency_ms=latency, kind="error"
            )
        return PollResult(True, status=status, latency_ms=latency)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return PollResult(False, error=type(exc).__name__, kind="unreachable")
    except Exception as exc:  # read timeout, hang, ...
        return PollResult(False, error=type(exc).__name__, kind="error")
