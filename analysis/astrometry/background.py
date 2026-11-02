"""
analysis/astrometry/background.py  (ported from ssa-toolkit photometry/background.py, object mode)
========================
Phase 1 preprocessing: background estimation, a per-pixel noise (RMS) map, and
column de-striping for the vertical fixed-pattern noise seen on the target
optical frames.

The two effects are separated deliberately:

  * The **background** (sky level + large-scale gradient from twilight / moon /
    light pollution) is estimated with a mesh wide enough that a single-column
    stripe is averaged out — so the background carries the gradient, NOT the
    fixed pattern.
  * The **stripe** (per-column additive offset) is then measured on the
    background-subtracted residual, with sources masked so stars/streaks don't
    bias it, and removed.

Order of operations (``reduce_frame``):
    1. coarse Background2D  -> gradient only (mesh averages across columns)
    2. residual = data - gradient ; mask bright sources
    3. per-column sigma-robust offset from the masked residual -> de-stripe
    4. final Background2D on the de-striped frame -> background + RMS map
    5. data_sub = de-striped - background   (ready for detection)

With ``destripe=False`` steps 1-3 are skipped and the frame passes straight to
the background estimate (a true no-op relative to the un-striped path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np


@dataclass
class BackgroundParams:
    destripe: bool = True
    destripe_axis: str = "columns"   # "columns" (vertical stripes) | "rows"
    box_size: int = 64               # background mesh cell (px)
    max_bkg_cells: int = 48          # cap Background2D mesh count per axis: on big
                                     # frames grow the box so cost stops scaling
                                     # with pixels (the sky gradient/vignette is
                                     # smooth, so a coarser mesh still tracks it).
                                     # Only bites above ~3072 px; <=0 disables.
    filter_size: int = 3             # median filter over the mesh
    sigma: float = 3.0               # sigma-clip for the background estimator
    mask_sigma: float = 3.0          # source-masking threshold for de-striping
    rms_floor_frac: float = 0.3      # floor the per-pixel RMS at this fraction of
                                     # the frame's typical sky RMS, so meshes that
                                     # collapse to ~0 noise (unexposed borders,
                                     # sky-subtracted products, flat black regions)
                                     # don't drive the detection threshold to 0 and
                                     # flag noise. 0 disables the floor.


@dataclass
class BackgroundResult:
    data_sub: np.ndarray             # background-subtracted (and de-striped) frame
    background: np.ndarray           # estimated background (gradient + sky level)
    rms: np.ndarray                  # per-pixel background RMS (noise) map
    destriped: bool
    destripe_offsets: Optional[np.ndarray] = None  # per-column (or -row) offsets
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def reduce_frame(image: Union[np.ndarray, "object"],
                 params: Optional[BackgroundParams] = None) -> BackgroundResult:
    """Background-subtract and (optionally) de-stripe *image*.

    ``image`` may be a 2-D float array or an :class:`analysis.astrometry.io.ImageData`.
    """
    params = params or BackgroundParams()
    data = np.asarray(getattr(image, "data", image), dtype=np.float64)
    if data.ndim != 2:
        raise ValueError(f"reduce_frame expects a 2-D frame, got {data.ndim}-D")

    warnings: list[str] = []
    offsets: Optional[np.ndarray] = None
    work = data

    if params.destripe:
        # 1. coarse gradient (mesh averages out single-column stripes)
        grad, grad_rms = _background2d(data, params, warnings)
        # 2. residual + source mask
        resid = data - grad
        src_mask = resid > (params.mask_sigma * grad_rms)
        # 3. per-column (or -row) robust offset, zero-meaned (level is the
        #    background's job, not the stripe's), then removed
        offsets = _stripe_offsets(resid, src_mask, params.destripe_axis)
        work = _apply_offsets(data, offsets, params.destripe_axis)

    # 4. final background + RMS on the (de-striped) frame
    background, rms = _background2d(work, params, warnings)
    rms = _floor_rms(rms, params.rms_floor_frac)
    # 5. detection-ready frame
    data_sub = work - background

    return BackgroundResult(
        data_sub=data_sub.astype(np.float32),
        background=background.astype(np.float32),
        rms=rms.astype(np.float32),
        destriped=bool(params.destripe),
        destripe_offsets=offsets,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _background2d(data: np.ndarray, params: BackgroundParams,
                  warnings: Optional[list] = None):
    """photutils Background2D -> (background, rms) maps, with edge-safe box.

    Falls back to a global sigma-clipped estimate only if Background2D fails
    (degenerate/tiny frame); the fallback is recorded in *warnings* so a real-
    frame failure is never silent."""
    from astropy.stats import SigmaClip
    from photutils.background import Background2D, MedianBackground

    h, w = data.shape
    # Box must (a) stay wide enough to average single-column stripes away, but
    # (b) be fine enough to give a mesh that can follow a background gradient.
    # Floor the mesh at ~8 cells per axis so small frames don't collapse to a
    # 4x4 grid whose median-filter smooths the gradient off at the edges; large
    # frames keep the requested box_size.
    mesh_floor = max(16, min(h, w) // 8)
    box = int(max(8, min(params.box_size, mesh_floor)))
    # (B) On large frames grow the box so the mesh count per axis is capped —
    # Background2D cost ~ mesh_count², so this keeps reduce-time bounded instead
    # of scaling with pixels. Never shrinks the small-frame box.
    if params.max_bkg_cells and params.max_bkg_cells > 0:
        box = max(box, int(np.ceil(max(h, w) / params.max_bkg_cells)))
    box = int(min(box, min(h, w)))
    fsz = int(max(1, min(params.filter_size, box)))

    try:
        bkg = Background2D(
            data,
            box_size=(box, box),
            filter_size=(fsz, fsz),
            sigma_clip=SigmaClip(sigma=params.sigma),
            bkg_estimator=MedianBackground(),
        )
        return np.asarray(bkg.background), np.asarray(bkg.background_rms)
    except Exception as exc:  # noqa: BLE001 — photutils raises many low-level types
        # Degenerate/tiny frame: fall back to a global estimate, but log it so a
        # real gradient being flattened doesn't pass unnoticed.
        from astropy.stats import sigma_clipped_stats
        if warnings is not None:
            warnings.append(
                f"Background2D failed ({type(exc).__name__}: {exc}); "
                "fell back to a global sigma-clipped background (no gradient tracking)"
            )
        _, median, std = sigma_clipped_stats(data, sigma=params.sigma)
        return (np.full_like(data, median, dtype=np.float64),
                np.full_like(data, max(std, 1e-6), dtype=np.float64))


def _floor_rms(rms: np.ndarray, floor_frac: float) -> np.ndarray:
    """Clamp the per-pixel RMS map up to ``floor_frac`` * the frame's typical sky
    RMS. photutils reports RMS ~ 0 for a mesh with no scatter (an unexposed
    border, a sky-subtracted product's zero regions, a black margin); an ~0 RMS
    makes the ``nsigma * rms`` detection threshold collapse to ~0 there, so pure
    noise/pattern residue gets flagged as sources. Flooring leaves normal sky
    untouched (its RMS is already >> the floor) and only lifts the degenerate
    near-zero meshes. ``floor_frac <= 0`` disables it."""
    if floor_frac <= 0:
        return rms
    positive = rms[rms > 0]
    if positive.size == 0:
        return rms
    floor = float(floor_frac * np.median(positive))
    return np.maximum(rms, floor)


def _stripe_offsets(resid: np.ndarray, src_mask: np.ndarray, axis: str) -> np.ndarray:
    """Per-column (axis='columns') or per-row offset = robust median of the
    source-masked residual, zero-meaned so only the *pattern* is removed."""
    masked = resid.copy()
    masked[src_mask] = np.nan

    # columns vary along x -> reduce over rows (axis 0); rows -> reduce over cols
    reduce_axis = 0 if axis == "columns" else 1
    with np.errstate(all="ignore"):
        offs = np.nanmedian(masked, axis=reduce_axis)
    offs = np.where(np.isfinite(offs), offs, 0.0)
    offs -= np.median(offs)              # remove overall level; keep the pattern
    return offs


def _apply_offsets(data: np.ndarray, offsets: np.ndarray, axis: str) -> np.ndarray:
    if axis == "columns":
        return data - offsets[np.newaxis, :]
    return data - offsets[:, np.newaxis]


# ---------------------------------------------------------------------------
# diagnostics (used by tests + tuning)
# ---------------------------------------------------------------------------

def column_pattern_amplitude(data: np.ndarray, mask_sigma: float = 3.0) -> float:
    """Std across columns of the source-masked per-column median — a scalar
    measure of residual vertical fixed-pattern striping. Lower == flatter."""
    from astropy.stats import sigma_clipped_stats
    _, median, std = sigma_clipped_stats(data, sigma=mask_sigma)
    masked = data.astype(np.float64).copy()
    masked[data > median + mask_sigma * std] = np.nan
    with np.errstate(all="ignore"):
        col_med = np.nanmedian(masked, axis=0)
    col_med = col_med[np.isfinite(col_med)]
    return float(np.std(col_med)) if col_med.size else 0.0
