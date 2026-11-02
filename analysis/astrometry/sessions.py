"""
analysis/astrometry/sessions.py  (new for pw-mount-control, T5.2a)
==================================================================
In-memory image sessions: an uploaded frame lives under an ``image_id`` (uuid4 hex)
so click-snap and the later RUN do not re-upload it.

* at most ``max_frames`` (3) frames - adding a fourth evicts the least recently used;
* ``ttl_s`` (30 min) idle TTL, measured from the last ``get``;
* thread-safe (one registry lock; each session has its own work lock for the lazy,
  expensive engine build);
* the clock is injectable so tests can fake time.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .io import ImageData

MAX_FRAMES = 3
IDLE_TTL_S = 30 * 60


@dataclass
class ImageSession:
    image_id: str
    image: ImageData
    meta: dict
    last_used: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    engine: Any = None              # DetectionEngine, built lazily by the first detect / snap
    result: Optional[dict] = None   # cached detect answer


class ImageSessions:
    def __init__(self, max_frames: int = MAX_FRAMES, ttl_s: float = IDLE_TTL_S,
                 clock: Callable[[], float] = time.monotonic):
        self.max_frames = max_frames
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, ImageSession] = {}

    def _purge_locked(self) -> None:
        now = self._clock()
        for k in [k for k, s in self._items.items() if now - s.last_used > self.ttl_s]:
            del self._items[k]

    def add(self, image: ImageData, meta: dict) -> str:
        with self._lock:
            self._purge_locked()
            while len(self._items) >= self.max_frames:
                oldest = min(self._items.values(), key=lambda s: s.last_used)
                del self._items[oldest.image_id]
            image_id = uuid.uuid4().hex
            self._items[image_id] = ImageSession(image_id, image, meta, self._clock())
            return image_id

    def get(self, image_id: str) -> Optional[ImageSession]:
        """The live session (its idle timer restarts), or None when unknown / expired."""
        with self._lock:
            self._purge_locked()
            s = self._items.get(image_id)
            if s is not None:
                s.last_used = self._clock()
            return s

    def drop(self, image_id: str) -> None:
        with self._lock:
            self._items.pop(image_id, None)

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._items)
