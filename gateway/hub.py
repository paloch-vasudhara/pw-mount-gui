"""WebSocket fan-out: per-client queue of size 1, latest message wins."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


class Client:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self.dropped = 0

    def offer(self, msg: str) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(msg)


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self.latest: str | None = None
        # T3.1 attaches the link-loss stop here.
        self.on_connect: list[Callable[[Client], None]] = []
        self.on_disconnect: list[Callable[[Client], None]] = []

    def add(self) -> Client:
        c = Client()
        if self.latest is not None:
            c.offer(self.latest)
        self.clients.add(c)
        for cb in self.on_connect:
            cb(c)
        return c

    def remove(self, c: Client) -> None:
        if c in self.clients:
            self.clients.discard(c)
            for cb in self.on_disconnect:
                cb(c)

    def publish(self, msg: str) -> None:
        """Never blocks, regardless of slow clients."""
        self.latest = msg
        for c in self.clients:
            c.offer(msg)
