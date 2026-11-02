"""
analysis/astrometry/detect.py  (ported from ssa-toolkit photometry/detect.py, object mode)
====================
Phase 2 detection: threshold + segmentation + deblending, wrapped so the rest of
the engine gets a clean list of measured :class:`Source` objects plus the
segmentation map (for overlays/QA).

Detection runs on a background-subtracted frame (``data_sub`` from
:func:`photometry.background.reduce_frame`) against its per-pixel RMS map, so the
threshold is ``nsigma * rms`` locally rather than a single global cut.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .morphology import Source, measure_sources


@dataclass
class DetectParams:
    nsigma: float = 5.0              # detection threshold, in units of local RMS
    min_area: int = 5                # min connected pixels (npixels)
    connectivity: int = 8            # 4 or 8
    deblend: bool = True
    deblend_nlevels: int = 32
    deblend_contrast: float = 0.001
    edge_margin: int = 5             # px from border -> "edge" flag
    saturation_adu: Optional[float] = None   # peak >= this -> "saturated" flag


@dataclass
class ExtractionResult:
    sources: list[Source]
    segment_map: np.ndarray          # labelled map (0 = background)
    params: DetectParams

    @property
    def n_sources(self) -> int:
        return len(self.sources)


def extract_sources(data_sub: np.ndarray,
                    rms: np.ndarray,
                    params: Optional[DetectParams] = None,
                    raw: Optional[np.ndarray] = None) -> ExtractionResult:
    """Detect, deblend and measure all sources in a background-subtracted frame.

    ``raw`` (the pre-subtraction frame) is optional; when given it lets
    saturation be judged on true detector ADU rather than the subtracted peak.
    """
    params = params or DetectParams()
    data_sub = np.asarray(data_sub, dtype=np.float64)
    rms = np.asarray(rms, dtype=np.float64)
    if raw is not None:
        raw = np.asarray(raw, dtype=np.float64)

    segd, blended = _segment(data_sub, rms, params)
    if segd is None:
        return ExtractionResult([], np.zeros(data_sub.shape, dtype=int), params)

    sources = measure_sources(data_sub, rms, segd, params, blended, raw=raw)
    # brightest first — the order target selection walks in Phase 4
    sources.sort(key=lambda s: s.flux, reverse=True)
    return ExtractionResult(sources, np.asarray(segd.data), params)


def _segment(data_sub, rms, params):
    """Return (deblended SegmentationImage | None, set-of-blended-labels)."""
    import warnings as _warnings

    from photutils.segmentation import deblend_sources, detect_sources
    from photutils.utils.exceptions import NoDetectionsWarning

    threshold = params.nsigma * rms
    with _warnings.catch_warnings():
        # An empty frame is a valid outcome (we return an empty result), not a
        # condition worth surfacing to the caller's logs.
        _warnings.simplefilter("ignore", NoDetectionsWarning)
        segm = detect_sources(data_sub, threshold, npixels=params.min_area,
                              connectivity=params.connectivity)
    if segm is None:
        return None, set()

    if not params.deblend:
        return segm, set()

    segd = deblend_sources(data_sub, segm, npixels=params.min_area,
                           nlevels=params.deblend_nlevels,
                           contrast=params.deblend_contrast)

    # A child is "blended" if its parent segment split into >1 child.
    parent, child = segm.data, segd.data
    m = child > 0
    pairs = set(zip(child[m].tolist(), parent[m].tolist(), strict=True))
    children_of: dict[int, set] = defaultdict(set)
    for c, p in pairs:
        children_of[p].add(c)
    blended = {c for kids in children_of.values() if len(kids) > 1 for c in kids}
    return segd, blended
