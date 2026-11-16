"""Remembered window geometry (%LOCALAPPDATA%/pw-mount-control/window.json)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("launcher")

MIN_SIZE = (1280, 600)
DEFAULT_SIZE = (1440, 900)
Screen = tuple[int, int, int, int]  # left, top, right, bottom of the virtual desktop


def sanitize(raw: object, screen: Screen) -> dict:
    """A usable {width, height, x?, y?, maximized} from whatever the file held."""
    d = raw if isinstance(raw, dict) else {}

    def num(key: str) -> int | None:
        v = d.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    w, h = num("width"), num("height")
    sw, sh = screen[2] - screen[0], screen[3] - screen[1]
    if w is None or h is None or w > sw + 40 or h > sh + 40:
        w, h = DEFAULT_SIZE
    w, h = max(w, MIN_SIZE[0]), max(h, MIN_SIZE[1])
    out: dict = {"width": w, "height": h, "maximized": d.get("maximized") is True}
    x, y = num("x"), num("y")
    # off-screen (monitor unplugged, minimised -32000): reset to the default placement
    if (
        x is not None
        and y is not None
        and x + 120 <= screen[2]
        and x + w - 120 >= screen[0]
        and screen[1] <= y <= screen[3] - 60
    ):
        out["x"], out["y"] = x, y
    return out


def load(path: Path, screen: Screen) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = None
    return sanitize(raw, screen)


def save(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log.warning("could not save the window state: %s", exc)
