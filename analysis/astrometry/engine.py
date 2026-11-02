"""
analysis/astrometry/engine.py  (ported from ssa-toolkit photometry/engine.py, object mode)
====================
Detection-engine orchestrator (object-track mode; the CLI was dropped).

:class:`DetectionEngine` runs background -> detect -> classify ONCE, then
``label()`` does streak-processing + role assignment, and ``measure_at`` is the
click-to-snap manual add.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Optional, Union

import numpy as np

from .background import BackgroundParams, reduce_frame
from .detect import DetectParams, extract_sources
from .io import ImageData, load_image
from .select import SelectParams, _brt_edges, assign_roles
from .streaks import ClassifyParams, classify_sources, process_streaks
from .types import MODE_OBJECT, ROLE_STAR, Detection, DetectionResult

_STARVED_NSIGMA = 3.0   # fallback detection threshold for near-empty frames
                        # (3σ recovers real under-exposed trails but leaves pure
                        # broadband-noise frames empty — verified on 00000724 vs
                        # 00000577; see astrometry_engine_logs.md tuning #2 round-3).


def image_stats(img: ImageData) -> dict:
    """Basic frame statistics for the ``--stats`` CLI / sanity checks."""
    d = img.data
    finite = d[np.isfinite(d)]
    return {
        "format": img.source_format,
        "dims": f"{img.width} x {img.height}",
        "bit_depth": img.bit_depth,
        "color_original": img.is_color_original,
        "exposure_s": img.exposure_s,
        "epoch": img.epoch,
        "min": float(finite.min()) if finite.size else None,
        "max": float(finite.max()) if finite.size else None,
        "median": float(np.median(finite)) if finite.size else None,
        "mean": float(finite.mean()) if finite.size else None,
    }


class DetectionEngine:
    """One-extraction detection engine with instant re-labelling.

    Construction runs the mode-independent work ONCE — background reduction,
    source extraction/measurement, and point/streak classification — and caches
    it. :meth:`label` then applies a tracking ``mode`` (streak template/repair
    for object-track, endpoint finalisation for sidereal) and assigns roles,
    re-using the cached base sources. Toggling the mode therefore never
    re-extracts (asserted in the tests via :attr:`n_extractions`).

    :meth:`measure_at` provides click-to-snap manual addition against the same
    cached frame.
    """

    def __init__(self, image: Union[str, ImageData, np.ndarray],
                 mode: str = MODE_OBJECT,
                 bkg_params: Optional[BackgroundParams] = None,
                 detect_params: Optional[DetectParams] = None,
                 classify_params: Optional[ClassifyParams] = None,
                 select_params: Optional[SelectParams] = None,
                 detect_max_px: int = 3500):
        img = _as_image(image)
        self.image = img
        self.dims = (img.width, img.height)          # FULL-resolution (W, H)
        self.meta = {"epoch": img.epoch, "exposure_s": img.exposure_s}
        self.mode = mode

        dp = detect_params or DetectParams()
        # Deblending is MODE-DEPENDENT, so the extraction is too. In object-track
        # the stars TRAIL: a trail's along-length brightness variation makes the
        # deblender shatter one trail into many chunks -> spurious points + split
        # streaks (see docs/astrometry_engine_logs.md tuning #2). So deblend is
        # forced OFF for object-track. Sidereal keeps it (stars are points that
        # genuinely need separating). Because the cached extraction now depends on
        # `mode`, a mode SWITCH should re-extract — construct a fresh engine for
        # the other mode (the API does this per request; the mockup re-POSTs).
        if mode == MODE_OBJECT:
            dp = dataclasses.replace(dp, deblend=False)
        self._dp = dp
        self._cp = classify_params or ClassifyParams()
        self._sp = select_params or SelectParams()

        # Full-res raw is kept for the preview and for full-resolution centroid
        # refinement; detection itself runs on a DOWNSAMPLED working image so
        # reduce/extract cost stops scaling with pixels on big sensors (61 MP).
        self.raw = np.asarray(img.data, dtype=np.float32)  # T5.2a: no float64 copy of 61 MP
        W, H = img.width, img.height
        self._f = max(1, int(np.ceil(max(W, H) / float(detect_max_px)))) if detect_max_px else 1
        work = self.raw if self._f == 1 else _block_mean(self.raw, self._f)
        self.work_dims = (work.shape[1], work.shape[0])

        # stages 1-2: background + RMS (once, on the working image)
        br = reduce_frame(work, bkg_params or BackgroundParams())
        self.data_sub = br.data_sub
        self.rms = br.rms
        self.work_raw = work
        self.background_rms = float(np.median(br.rms))
        self._warnings = list(br.warnings)

        # High-noise metric (used by label() to warn only when it actually bites):
        # RMS as a fraction of the sky level. A clean-but-JPEG frame can have a high
        # ratio yet detect perfectly, so the WARNING is gated on weak detection in
        # label(), not on the ratio alone.
        bkg_med = float(np.median(br.background)) if br.background is not None else 0.0
        self._noise_ratio = self.background_rms / bkg_med if bkg_med > 0 else 0.0
        if self._f > 1:
            self._warnings.append(
                f"detection ran at 1/{self._f} scale ({self.work_dims[0]}x"
                f"{self.work_dims[1]}) for speed; target + star centroids refined "
                f"at full resolution")

        # stages 3-5: detect + measure + classify (once, mode-independent)
        ex = extract_sources(self.data_sub, self.rms, self._dp, raw=work)
        self.segment_map = ex.segment_map
        self._base_sources = ex.sources
        classify_sources(self._base_sources, self._cp)   # sets .kind in place
        self.n_extractions = 1   # primary 5-sigma extraction count (asserted == 1)

        # Starvation fallback: an under-exposed / low-dynamic-range frame (e.g. an
        # 8-bit JPEG with a high, noisy sky) can hide real trails just under the 5σ
        # cut. If the primary pass is essentially empty, retry ONCE at a reduced
        # threshold and ADOPT it only when it recovers genuine strong streaks — so a
        # truly source-starved frame (broadband noise) stays empty rather than
        # promoting noise blobs to detections.
        if len(self._base_sources) < 3 and self._dp.nsigma > _STARVED_NSIGMA:
            fb_dp = dataclasses.replace(self._dp, nsigma=_STARVED_NSIGMA)
            fb = extract_sources(self.data_sub, self.rms, fb_dp, raw=work)
            fb_sources = fb.sources
            classify_sources(fb_sources, self._cp)
            strong = [s for s in fb_sources if s.kind == "streak"
                      and s.length_px >= 20 and s.elongation >= 3.0 and s.snr >= 8.0]
            if len(strong) >= 3:
                self._base_sources = fb_sources
                self.segment_map = fb.segment_map
                self._dp = fb_dp
                self.n_extractions += 1
                self._warnings.append(
                    f"frame appears under-exposed / low dynamic range; detection used "
                    f"a reduced {_STARVED_NSIGMA:g}σ threshold to recover faint trails")

    # -- labelling --------------------------------------------------------
    def label(self, mode: Optional[str] = None) -> DetectionResult:
        """Apply a tracking ``mode`` to the cached sources and assign roles.
        Does NOT re-run the primary extraction. Defaults to the construction-time
        mode; the deblend setting is fixed at construction (see __init__), so
        re-labelling to the OTHER mode reuses the cached (deblend-per-construction)
        sources — construct a fresh engine when the tracking mode truly changes."""
        if mode is None:
            mode = self.mode
        h, w = self.data_sub.shape
        # deep-copy so streak repair/endpoint edits never mutate the cache
        srcs = copy.deepcopy(self._base_sources)

        # (C) Speed + declutter: streak processing is super-linear in source count
        # and ~60 stars already saturate a solve. Cap the working set to the
        # BRIGHTEST few (bright stars + the target survive; faint clutter drops)
        # so template/merge/repair stay cheap on dense frames.
        cap = max(200, 4 * self._sp.max_stars) if self._sp.max_stars else None
        if cap and len(srcs) > cap:
            srcs = sorted(srcs, key=lambda s: s.flux, reverse=True)[:cap]

        # ...and skip the object-track faint-recovery pass (a 2nd, low-threshold
        # extraction) when we already have plenty of bright streaks — we don't
        # need fainter ones, and it's the other big cost on dense frames.
        cp = self._cp
        if mode == MODE_OBJECT and self._sp.max_stars:
            n_streak = sum(1 for s in srcs if s.kind == "streak")
            if n_streak >= self._sp.max_stars:
                cp = dataclasses.replace(self._cp, matched_filter=False)

        srcs, template = process_streaks(
            self.data_sub, self.rms, srcs, self.segment_map, mode, (h, w),
            cp, self._dp)
        self.last_template = template   # exposes .recon_diag for diagnostics
        # role assignment happens in WORKING coords (center_bias etc. are scale-
        # invariant); then scale detections back to full-res and refine.
        res = assign_roles(srcs, mode, template, self.work_dims, self.meta,
                           self.background_rms, self._sp, self._warnings)
        if self._f > 1:
            self._scale_and_refine(res, self._f)
        else:
            res.image_dims = self.dims

        # High-noise warning — only when a noisy frame ALSO under-detects (a clean
        # JPEG can be "noisy" by ratio yet detect fine, so don't cry wolf there).
        if (self._noise_ratio >= 0.18
                and res.n_stars < self._sp.min_stars):
            res.warnings.append(
                f"high-noise frame (background RMS ~{self._noise_ratio*100:.0f}% of "
                f"the sky level) with few detections — faint trails are likely "
                f"under-detected and any detections may be unreliable; review carefully")
        return res

    def _scale_and_refine(self, res: DetectionResult, f: int) -> None:
        """Map detections from the 1/f working image back to full resolution and
        refine each solve-critical centroid on the full-res frame. Points refine
        their centre; streaks refine their MIDPOINT (what the object-track solve
        uses) and shift rigidly, preserving the fitted length/angle."""
        res.image_dims = self.dims
        if res.dominant_streak and res.dominant_streak.get("length_px") is not None:
            res.dominant_streak["length_px"] *= f
        for d in res.detections:
            if d.shape == "point" and d.x is not None:
                d.x, d.y = self._refine(d.x * f, d.y * f, f)
            elif d.shape == "streak" and d.xs is not None:
                d.xs, d.ys, d.xe, d.ye = d.xs * f, d.ys * f, d.xe * f, d.ye * f
                if d.length_px is not None:
                    d.length_px *= f
                mx, my = (d.xs + d.xe) / 2.0, (d.ys + d.ye) / 2.0
                rx, ry = self._refine(mx, my, f)
                dx, dy = rx - mx, ry - my
                d.xs += dx
                d.ys += dy
                d.xe += dx
                d.ye += dy

    def _refine(self, x: float, y: float, f: int) -> tuple[float, float]:
        """Full-res, local-background-subtracted flux-weighted centroid in a
        window sized to the working pixel's full-res footprint. Returns the input
        unchanged if there's no clear signal there."""
        r = int(2 * f + 2)
        h, w = self.raw.shape
        # clamp into [0, w]/[0, h]; an off-frame point (e.g. an edge streak's
        # extended midpoint) has no full-res neighbourhood to refine against
        x0, x1 = int(min(max(0, x - r), w)), int(min(max(0, x + r + 1), w))
        y0, y1 = int(min(max(0, y - r), h)), int(min(max(0, y + r + 1), h))
        if x1 <= x0 or y1 <= y0:
            return float(x), float(y)
        p = self.raw[y0:y1, x0:x1]
        if p.size == 0:
            return float(x), float(y)
        bg = float(np.median(p))
        pos = p - bg
        pos[pos < 0] = 0.0
        if pos.sum() <= 0:
            return float(x), float(y)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        return float((xx * pos).sum() / pos.sum()), float((yy * pos).sum() / pos.sum())

    # -- manual click-to-snap --------------------------------------------
    def measure_at(self, x: float, y: float, role: str = ROLE_STAR,
                   snap_search_px: float = 6.0, window: int = 9) -> Detection:
        """Snap a click at ``(x, y)`` to the nearest cached source; if none is
        within ``snap_search_px`` measure a fresh local centroid there. The
        returned point lands within ~2 px of a real source's centre when one is
        nearby (the snap guarantee)."""
        # (x, y) are FULL-res coords; base source centroids are working-scale, so
        # compare/snap in full-res by scaling them up by _f.
        f = self._f
        best, best_d = None, np.inf
        for s in self._base_sources:
            d = float(np.hypot(s.x * f - x, s.y * f - y))
            if d < best_d:
                best, best_d = s, d
        edges = _brt_edges(np.array([s.flux for s in self._base_sources], dtype=float))
        from .select import _brt, _confidence
        if best is not None and best_d <= snap_search_px * max(1, f):
            rx, ry = self._refine(best.x * f, best.y * f, f)
            return Detection(id="M1", role=role, shape="point", x=rx, y=ry,
                             brt=_brt(best.flux, edges), flux=float(best.flux),
                             snr=float(best.snr), fwhm=float(best.fwhm),
                             confidence=_confidence(best.snr, self._sp.conf_scale),
                             flags=["manual", "snapped"])
        return self._measure_local(x, y, role, window)

    def _measure_local(self, x: float, y: float, role: str, window: int) -> Detection:
        """Full-res flux-weighted local centroid around a fresh click, with a
        local background subtraction. Returns the click unchanged (unverified) if
        there's no clear peak there."""
        from .select import _confidence

        h, w = self.raw.shape
        r = max(2, window // 2, 2 * self._f)
        x0, x1 = int(min(max(0, x - r), w)), int(min(max(0, x + r + 1), w))
        y0, y1 = int(min(max(0, y - r), h)), int(min(max(0, y + r + 1), h))
        if x1 <= x0 or y1 <= y0:
            from .select import _confidence
            return Detection(id="M1", role=role, shape="point", x=float(x), y=float(y),
                             confidence=0.0, flags=["manual", "unverified"])
        patch = np.asarray(self.raw[y0:y1, x0:x1], dtype=np.float64)
        cx, cy, snr, verified = float(x), float(y), 0.0, False
        if patch.size:
            bg = float(np.median(patch))
            noise = float(np.std(patch)) or 1.0
            pos = patch - bg
            # only a real peak (>~5 sigma over the local background) counts
            if float(pos.max()) >= 5.0 * noise:
                pos[pos < 0] = 0.0
                if pos.sum() > 0:
                    yy, xx = np.mgrid[y0:y1, x0:x1]
                    cx = float((xx * pos).sum() / pos.sum())
                    cy = float((yy * pos).sum() / pos.sum())
                    snr = float(pos.sum() / (noise * np.sqrt(max(patch.size, 1))))
                    verified = True
        flags = ["manual"] if verified else ["manual", "unverified"]
        return Detection(id="M1", role=role, shape="point", x=cx, y=cy,
                         snr=snr, confidence=_confidence(snr, self._sp.conf_scale),
                         flags=flags)


def detect_image(image: Union[str, ImageData, np.ndarray],
                 mode: str = MODE_OBJECT, **kw) -> DetectionResult:
    """Shortcut: build a :class:`DetectionEngine` and label it once. ``mode`` is
    passed to construction so the deblend setting matches the tracking mode."""
    return DetectionEngine(image, mode=mode, **kw).label(mode)


def _block_mean(a: np.ndarray, f: int) -> np.ndarray:
    """Downsample by an integer factor via block averaging (crops the ragged
    right/bottom edge to a multiple of f). Averaging preserves faint-source SNR
    better than striding."""
    h, w = a.shape
    h2, w2 = h // f, w // f
    a = a[: h2 * f, : w2 * f]
    return a.reshape(h2, f, w2, f).mean(axis=(1, 3))


def _as_image(image: Union[str, ImageData, np.ndarray]) -> ImageData:
    """Accept a path, an :class:`ImageData`, or a bare 2-D array."""
    if isinstance(image, ImageData):
        return image
    if isinstance(image, np.ndarray):
        return ImageData(data=np.asarray(image, dtype=np.float32),
                         width=image.shape[1], height=image.shape[0],
                         source_format="array")
    return load_image(str(image))
