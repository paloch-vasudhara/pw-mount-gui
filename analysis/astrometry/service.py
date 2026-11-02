"""
analysis/astrometry/service.py  (new for pw-mount-control, T5.2a)
=================================================================
The synchronous work behind ``/api/astrometry/{image,detect,snap}``: metadata block,
stretched preview, detection / snap in ORIGINAL image pixels. Shapes follow
ssa-toolkit ``data_api/routes/astrometry.py`` (``_meta`` / ``_preview_from_raw``,
``/measure``), trimmed to the T5.2 spec.

Helpers T5.2b (solve / nova / jobs) builds on:
  * :func:`det_midpoint`    - (x, y) of a detection dict (the streak midpoint = the
                              star position at mid-exposure, or the target point)
  * :func:`star_midpoints`  - [(x, y), ...] of all star detections in a list
  * :func:`target_point`    - (x, y) of the target detection, or None
"""

from __future__ import annotations

import base64
import io
import re
from typing import Optional

import numpy as np

from .engine import DetectionEngine
from .io import ImageData
from .sessions import ImageSession
from .types import MODE_OBJECT, ROLE_STAR, ROLE_TARGET, Detection

PREVIEW_MAX_PX = 1600

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?")


def normalize_epoch(raw: Optional[str]) -> Optional[str]:
    """DATE-OBS / EXIF string -> ``YYYY-MM-DDTHH:MM:SS[.fff]Z`` (UTC), else None."""
    if not raw:
        return None
    m = _ISO_RE.match(str(raw).strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}{frac or ''}Z"


def _num(v, lo: float, hi: float) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) and lo < f < hi else None


def build_meta(img: ImageData) -> dict:
    """``{format, bit_depth, dims, epoch_iso, exposure_s, focal_mm, pixel_um, warnings}``."""
    warnings = list(img.warnings)
    epoch_iso = normalize_epoch(img.epoch)
    if img.epoch and epoch_iso is None:
        warnings.append("capture time in the file is not understood - enter it manually")
    elif epoch_iso and img.source_format != "fits":
        warnings.append("capture time from the file is taken as UTC - check it")
    hdr = img.header or {}
    return {
        "format": img.source_format,
        "bit_depth": img.bit_depth,
        "dims": [img.width, img.height],
        "epoch_iso": epoch_iso,
        "exposure_s": _num(img.exposure_s, 0.0, 1e5),
        "focal_mm": _num(hdr.get("FOCALLEN"), 0.0, 1e5),
        "pixel_um": _num(hdr.get("XPIXSZ"), 0.0, 1e3),
        "warnings": warnings,
    }


def make_preview(data: np.ndarray, max_px: int = PREVIEW_MAX_PX) -> dict:
    """asinh-stretched PNG, long side <= ``max_px``; ``scale`` = preview / original."""
    from PIL import Image

    h0, w0 = data.shape
    k = min(1.0, max_px / float(max(w0, h0)))
    pw, ph = max(1, round(w0 * k)), max(1, round(h0 * k))
    im = Image.fromarray(np.ascontiguousarray(data, dtype=np.float32), mode="F")
    if (pw, ph) != (w0, h0):
        im = im.resize((pw, ph), Image.BOX)
    d = np.asarray(im, dtype=np.float32)
    lo, hi = np.percentile(d, 1.0), np.percentile(d, 99.8)
    if hi <= lo:
        hi = lo + 1.0
    x = np.clip((d - lo) / (hi - lo), 0, 1)
    x = np.arcsinh(x * 10.0) / np.arcsinh(10.0)
    buf = io.BytesIO()
    Image.fromarray((x * 255).astype("uint8")).save(buf, format="PNG")
    return {
        "png_b64": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
        "width": pw, "height": ph, "scale": pw / float(w0),
    }


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 2)


def det_to_dict(d: Detection, did: Optional[str] = None) -> dict:
    """Spec shape. Streaks also carry x, y = their midpoint; points have null xs..ye."""
    if d.shape == "streak":
        mid = d.midpoint()
        x, y = (mid if mid else (None, None))
    else:
        x, y = d.x, d.y
    return {
        "id": did or d.id, "role": d.role, "shape": d.shape,
        "x": _r(x), "y": _r(y),
        "xs": _r(d.xs), "ys": _r(d.ys), "xe": _r(d.xe), "ye": _r(d.ye),
        "brt": int(d.brt),
    }


def _engine(s: ImageSession) -> DetectionEngine:
    if s.engine is None:
        s.engine = DetectionEngine(s.image, mode=MODE_OBJECT)
    return s.engine


def detect(s: ImageSession) -> dict:
    with s.lock:
        if s.result is None:
            res = _engine(s).label(MODE_OBJECT)
            stars = [d for d in res.detections if d.role == ROLE_STAR]
            tgt = res.target
            out = [det_to_dict(d, f"S{i}") for i, d in enumerate(stars, 1)]
            tdict = det_to_dict(tgt, "T") if tgt is not None else None
            if tdict:
                out.insert(0, tdict)
            s.result = {"detections": out, "n_streaks": len(stars), "target": tdict,
                        "warnings": list(res.warnings)}
        return s.result


def snap(s: ImageSession, x: float, y: float, role: str) -> dict:
    with s.lock:
        d = _engine(s).measure_at(x, y, role=role)
    return det_to_dict(d, "T" if role == ROLE_TARGET else "S?")


# -- helpers for T5.2b -------------------------------------------------------
def det_midpoint(d: dict) -> Optional[tuple[float, float]]:
    """(x, y) of a detection dict from the API: streak midpoint, else the point."""
    if d.get("shape") == "streak" and None not in (d.get("xs"), d.get("ys"),
                                                    d.get("xe"), d.get("ye")):
        return ((d["xs"] + d["xe"]) / 2.0, (d["ys"] + d["ye"]) / 2.0)
    if d.get("x") is not None and d.get("y") is not None:
        return (float(d["x"]), float(d["y"]))
    return None


def star_midpoints(dets: list[dict]) -> list[tuple[float, float]]:
    pts = [det_midpoint(d) for d in dets if d.get("role") == ROLE_STAR]
    return [p for p in pts if p is not None]


def target_point(dets: list[dict]) -> Optional[tuple[float, float]]:
    for d in dets:
        if d.get("role") == ROLE_TARGET:
            return det_midpoint(d)
    return None
