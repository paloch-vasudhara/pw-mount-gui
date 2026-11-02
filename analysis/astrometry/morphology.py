"""
analysis/astrometry/morphology.py  (ported from ssa-toolkit photometry/morphology.py, object mode)
========================
Phase 2 measurement: per-source shape + photometry from a (deblended)
segmentation map.  Produces :class:`Source` objects — the raw, mode-agnostic
measurements that Phase 3 classifies (point vs streak) and Phase 4 turns into
:class:`photometry.types.Detection`.

Each source carries: sub-pixel centroid, flux, peak, area, the intensity second
moments (semimajor/minor sigma), elongation, orientation, the projected
length/width extents (true pixel span along the major/minor axes — reused for
streak endpoints later), an FWHM estimate, a flux-based SNR, and QA flags
(edge / saturated / blended).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Source:
    label: int
    x: float                 # centroid (sub-pixel)
    y: float
    flux: float              # segment flux (background-subtracted)
    peak: float              # max pixel value
    area: float              # segment area (px)
    a_sigma: float           # semimajor 1-sigma (px)
    b_sigma: float           # semiminor 1-sigma (px)
    elongation: float        # a_sigma / b_sigma
    orientation_deg: float   # major-axis angle from +x, CCW, (-90, 90]
    length_px: float         # projected extent along major axis
    width_px: float          # projected extent along minor axis
    fwhm: float              # Gaussian-equivalent FWHM (px)
    snr: float               # flux / (rms * sqrt(area))
    bbox: tuple[int, int, int, int]  # (xmin, ymin, xmax, ymax)
    blended: bool = False
    flags: list[str] = field(default_factory=list)

    # Phase 3: point/streak label + streak endpoints (centroid +- L/2 along the
    # major axis; repaired to the consensus length where applicable).
    kind: Optional[str] = None       # "point" | "streak"
    xs: Optional[float] = None
    ys: Optional[float] = None
    xe: Optional[float] = None
    ye: Optional[float] = None

    # TRUE measured endpoints (extreme pixels along the major axis) — captured at
    # measurement time and NEVER template-extended, so an edges-export WCS match
    # uses the real trail ends. xs/ys/xe/ye may be synthesised (symmetric extend);
    # these stay the ground truth.
    xs_m: Optional[float] = None
    ys_m: Optional[float] = None
    xe_m: Optional[float] = None
    ye_m: Optional[float] = None


def measure_sources(data_sub: np.ndarray,
                    rms: np.ndarray,
                    segm,
                    params,
                    blended_labels: Optional[set] = None,
                    raw: Optional[np.ndarray] = None) -> list[Source]:
    """Measure every labelled source in *segm* (a photutils SegmentationImage).

    ``raw`` is the pre-background-subtraction frame; when supplied, saturation is
    tested against the RAW pixel peak (``saturation_adu`` is a detector-ADU
    level), not the background-subtracted peak which would under-flag on a sky
    pedestal.  Falls back to the subtracted peak if ``raw`` is None.
    """
    from photutils.segmentation import SourceCatalog

    blended_labels = blended_labels or set()
    h, w = data_sub.shape
    cat = SourceCatalog(data_sub, segm)

    labels = np.atleast_1d(cat.labels)
    xc = np.atleast_1d(cat.xcentroid)
    yc = np.atleast_1d(cat.ycentroid)
    flux = np.atleast_1d(cat.segment_flux)
    peak = np.atleast_1d(cat.max_value)
    area = np.atleast_1d(_val(cat.area))
    a_sig = np.atleast_1d(_val(cat.semimajor_sigma))
    b_sig = np.atleast_1d(_val(cat.semiminor_sigma))
    elong = np.atleast_1d(_val(cat.elongation))
    orient = np.atleast_1d(_val(cat.orientation))
    fwhm = np.atleast_1d(_val(cat.fwhm))
    slices = cat.slices

    seg_data = segm.data
    sources: list[Source] = []
    for i, lab in enumerate(labels):
        sy, sx = slices[i]
        sub = seg_data[sy, sx] == lab
        ys, xs = np.nonzero(sub)
        ys = ys + sy.start
        xs = xs + sx.start

        theta = np.deg2rad(_safe(orient[i], 0.0))
        length, width = _projected_extents(xs, ys, xc[i], yc[i], theta)
        ep = _extreme_endpoints(xs, ys, theta)   # true major-axis end pixels

        # flux-based SNR using the noise sampled inside the segment
        seg_rms = float(np.mean(rms[ys, xs])) if ys.size else float(np.median(rms))
        a = max(float(area[i]), 1.0)
        snr = float(flux[i] / (seg_rms * np.sqrt(a))) if seg_rms > 0 else 0.0

        xmin, xmax = int(xs.min()), int(xs.max())
        ymin, ymax = int(ys.min()), int(ys.max())

        flags: list[str] = []
        # Dynamic edge margin: a trail whose last above-threshold pixel lands a
        # little inside a fixed 5 px border was slipping through unflagged, so the
        # margin also scales with frame size (0.5% of the short side).
        margin = max(params.edge_margin, int(round(0.005 * min(h, w))))
        if xmin < margin or ymin < margin or xmax >= w - margin or ymax >= h - margin:
            flags.append("edge")
        if params.saturation_adu is not None:
            sat_peak = float(raw[ys, xs].max()) if raw is not None and ys.size else peak[i]
            if sat_peak >= params.saturation_adu:
                flags.append("saturated")

        blended = int(lab) in blended_labels
        if blended:
            flags.append("blended")

        sources.append(Source(
            label=int(lab), x=float(xc[i]), y=float(yc[i]),
            flux=float(flux[i]), peak=float(peak[i]), area=a,
            a_sigma=_safe(a_sig[i], 0.0), b_sigma=_safe(b_sig[i], 0.0),
            elongation=_safe(elong[i], 1.0), orientation_deg=_safe(orient[i], 0.0),
            length_px=length, width_px=width, fwhm=_safe(fwhm[i], 0.0),
            snr=snr, bbox=(xmin, ymin, xmax, ymax),
            blended=blended, flags=flags,
            xs_m=ep[0], ys_m=ep[1], xe_m=ep[2], ye_m=ep[3],
        ))
    return sources


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _projected_extents(xs, ys, cx, cy, theta) -> tuple[float, float]:
    """Pixel span along the major (length) and minor (width) axes."""
    if xs.size == 0:
        return 0.0, 0.0
    ct, st = np.cos(theta), np.sin(theta)
    dx, dy = xs - cx, ys - cy
    p = dx * ct + dy * st       # along major axis
    q = -dx * st + dy * ct      # along minor axis
    length = float(p.max() - p.min()) + 1.0   # inclusive pixel span
    width = float(q.max() - q.min()) + 1.0
    return length, width


def _extreme_endpoints(xs, ys, theta) -> tuple:
    """The two pixels at the extreme ends of the major axis — the TRUE trail
    endpoints. Returns (xs_m, ys_m, xe_m, ye_m); for a compact point both ends
    collapse to ~the centroid pixel."""
    if xs.size == 0:
        return (None, None, None, None)
    ct, st = np.cos(theta), np.sin(theta)
    p = xs * ct + ys * st                  # projection along major axis
    i0, i1 = int(np.argmin(p)), int(np.argmax(p))
    return (float(xs[i0]), float(ys[i0]), float(xs[i1]), float(ys[i1]))


def _val(x):
    """Strip astropy Quantity units if present."""
    return getattr(x, "value", x)


def _safe(v, default):
    v = float(v)
    return v if np.isfinite(v) else default
