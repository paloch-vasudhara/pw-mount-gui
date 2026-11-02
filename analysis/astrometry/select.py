"""
analysis/astrometry/select.py  (ported from ssa-toolkit photometry/select.py, object mode)
====================
Phase 4: role assignment, target selection, and the ``Source`` -> ``Detection``
conversion that produces the engine's public :class:`DetectionResult`.

The base source extraction (Phases 1-3) is **mode-independent**; this module is
where the tracking ``mode`` finally matters:

  * **object-track**  — the satellite is a POINT, the stars TRAIL. Target = the
    brightest confident point; stars = the streaks (their MIDPOINTS feed the
    plate solve).
  * **sidereal-track** — the satellite is the STREAK, the stars are POINTS.
    Target = the (brightest) streak; stars = the points.

Target selection walks the target-class candidates brightest-first and keeps the
confident ones as ALTERNATES for manual re-pick; the brightest is the target.
A frame with fewer than ``min_stars`` usable stars raises a SOFT warning
(continuable — "you have been warned"), never a hard failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .morphology import Source
from .streaks import StreakTemplate
from .types import (
    BRT_MAX,
    BRT_MIN,
    MODE_OBJECT,
    ROLE_STAR,
    ROLE_TARGET,
    SHAPE_POINT,
    SHAPE_STREAK,
    Detection,
    DetectionResult,
)


@dataclass
class SelectParams:
    min_stars: int = 10              # SOFT floor for a valid solve (warn, continue)
    max_stars: int = 60              # HARD cap on emitted stars: ~50-60 stars is
                                     # already plenty for a reliable plate solve,
                                     # and more just clutter the Detections overlay.
                                     # Keep the brightest; drop fainter. <=0 = no cap.
    conf_scale: float = 5.0          # SNR -> confidence softness (see _confidence)
    alt_conf_thresh: float = 0.6     # an alternate target must be at least this confident
    max_alternates: int = 5          # cap the surfaced alternate picks
    target_max_elong: float = 1.8    # object-track: the tracked target is a POINT,
                                     # so a target candidate must be ROUND. Elongated
                                     # points (off-axis short trails, planes, image
                                     # marks) are excluded from target candidacy so a
                                     # bright elongated artefact can't outrank the
                                     # round satellite. Matches ClassifyParams'
                                     # elongation_thresh.
    center_bias: float = 0.0         # object-track prior: tracked targets usually sit
                                     # near field centre. 0 = pure brightest (the P4
                                     # default). >0 down-weights off-centre candidates
                                     # so a central, comparably-bright point wins;
                                     # a clearly brighter point still dominates. Tune
                                     # on real frames (P6). Only affects object-track
                                     # point targets; never overrides SNR by much.


# ---------------------------------------------------------------------------
# confidence + brightness quantisation
# ---------------------------------------------------------------------------

def _confidence(snr: float, scale: float) -> float:
    """Map a flux SNR to a 0..1 detection confidence. Saturating: SNR≈scale ->
    ~0.63, SNR≈3·scale -> ~0.95. All extracted sources already cleared the
    nsigma detection cut, so this ranks how *trustworthy* a source is."""
    return float(np.clip(1.0 - np.exp(-max(snr, 0.0) / scale), 0.0, 1.0))


def _brt_edges(fluxes: np.ndarray) -> np.ndarray:
    """Quintile edges of the emitted fluxes -> the modal's 1..5 brt column."""
    if fluxes.size == 0:
        return np.array([])
    return np.quantile(fluxes, [0.2, 0.4, 0.6, 0.8])


def _brt(flux: float, edges: np.ndarray) -> int:
    if edges.size == 0:
        return BRT_MIN
    return int(np.clip(BRT_MIN + int(np.searchsorted(edges, flux)), BRT_MIN, BRT_MAX))


# ---------------------------------------------------------------------------
# Source -> Detection
# ---------------------------------------------------------------------------

def source_to_detection(src: Source, did: str, role: str, params: SelectParams,
                        brt_edges: np.ndarray, extra_flags: Optional[list] = None) -> Detection:
    """Convert a measured/classified :class:`Source` into a public
    :class:`Detection`. Shape follows the source's ``kind`` (point vs streak);
    a streak carries its endpoints so ``.midpoint()`` is available for the
    object-track plate solve."""
    is_streak = src.kind == "streak"
    flags = list(src.flags) + list(extra_flags or [])
    d = Detection(
        id=did, role=role,
        shape=SHAPE_STREAK if is_streak else SHAPE_POINT,
        brt=_brt(src.flux, brt_edges),
        flux=float(src.flux), snr=float(src.snr), fwhm=float(src.fwhm),
        confidence=_confidence(src.snr, params.conf_scale), flags=flags,
    )
    if is_streak:
        d.xs, d.ys = _f(src.xs), _f(src.ys)
        d.xe, d.ye = _f(src.xe), _f(src.ye)
        d.length_px = float(src.length_px)
        d.angle_deg = float(src.orientation_deg)
    else:
        d.x, d.y = float(src.x), float(src.y)
    return d


def _f(v):
    return None if v is None else float(v)


# ---------------------------------------------------------------------------
# role assignment / target selection
# ---------------------------------------------------------------------------

def assign_roles(sources: list[Source], mode: str,
                 template: Optional[StreakTemplate],
                 dims: tuple[int, int],
                 meta: dict,
                 background_rms: Optional[float],
                 params: Optional[SelectParams] = None,
                 base_warnings: Optional[list] = None) -> DetectionResult:
    """Assign target/star roles by ``mode`` and build the DetectionResult.

    object  -> target = brightest confident POINT ; stars = STREAKS (midpoints).
    sidereal-> target = brightest confident STREAK ; stars = POINTS.
    Edge-clipped star streaks keep their "edge" flag so a downstream midpoint
    plate solve can exclude them (their interior endpoint is still usable for an
    endpoint-based solve)."""
    params = params or SelectParams()
    warnings: list[str] = list(base_warnings or [])

    is_object = mode == MODE_OBJECT
    target_kind = "point" if is_object else "streak"
    star_kind = "streak" if is_object else "point"

    # Rank target candidates by brightness, optionally biased toward field centre
    # for object-track (tracked target usually near centre). center_bias=0 -> pure
    # brightest (P4 default); the flux weight keeps a clearly brighter source on top.
    def _rank(s: Source) -> float:
        score = float(s.flux)
        if is_object and params.center_bias > 0 and dims:
            w, h = dims
            dn = np.hypot(s.x - w / 2.0, s.y - h / 2.0) / (0.5 * np.hypot(w, h))
            score *= max(0.0, 1.0 - params.center_bias * float(dn))
        return score

    # object-track target candidates must be ROUND points (a trailed/elongated
    # source is never the tracked point target). Sidereal target is a streak.
    def _is_target_candidate(s: Source) -> bool:
        if s.kind != target_kind:
            return False
        if is_object and s.elongation > params.target_max_elong:
            return False
        return True

    candidates = sorted((s for s in sources if _is_target_candidate(s)),
                        key=_rank, reverse=True)
    stars = [s for s in sources if s.kind == star_kind]

    # Object-track: a frame-clipped star trail is length-truncated, so its
    # midpoint (what the plate solve uses) is unreliable — invalidate it as a
    # solve star rather than emitting a misleading half-streak (the "S5" edge
    # case). Sidereal keeps all point stars.
    n_edge = 0
    if is_object:
        kept = [s for s in stars if "edge" not in s.flags]
        n_edge = len(stars) - len(kept)
        stars = kept

    # brightness quintiles over everything we will emit
    emitted = candidates[: 1 + params.max_alternates] + stars
    edges = _brt_edges(np.array([s.flux for s in emitted], dtype=float))

    detections: list[Detection] = []

    # target + alternates (brightest-first, alternates gated on confidence)
    target_src = candidates[0] if candidates else None
    if target_src is not None:
        detections.append(source_to_detection(target_src, "T1", ROLE_TARGET, params, edges))
    else:
        warnings.append(f"no target candidate found in {mode}-track geometry "
                        f"(no {target_kind} sources)")

    # Alternates (surfaced for manual re-pick) are only emitted in SIDEREAL mode.
    # In object-track the stars are STREAKS — emitting extra round POINTS as
    # "alternate" candidates just clutters the overlay with non-stars (the user
    # sees phantom points in a streak field). The candidate COUNT is still
    # reported via n_target_candidates; a wrong auto-target is re-picked by click.
    n_alt = 0
    if not is_object:
        for c in candidates[1:]:
            if n_alt >= params.max_alternates:
                break
            if _confidence(c.snr, params.conf_scale) < params.alt_conf_thresh:
                break  # walking brightest-down; once confidence drops, stop
            n_alt += 1
            detections.append(source_to_detection(
                c, f"A{n_alt}", ROLE_STAR, params, edges, extra_flags=["alternate"]))

    # stars (the solve set; brightest-first) — capped so the overlay stays legible
    stars_sorted = sorted(stars, key=lambda s: s.flux, reverse=True)
    n_detected = len(stars_sorted)
    if params.max_stars and n_detected > params.max_stars:
        stars_sorted = stars_sorted[: params.max_stars]
        warnings.append(
            f"showing the brightest {params.max_stars} of {n_detected} detected stars "
            f"(more than enough for a solve; fainter ones dropped to keep the display clear)")
    for i, s in enumerate(stars_sorted, start=1):
        detections.append(source_to_detection(s, f"S{i}", ROLE_STAR, params, edges))

    if n_edge:
        warnings.append(
            f"excluded {n_edge} frame-clipped (edge) star trail(s) from the solve "
            f"set — their truncated endpoints make the midpoint unreliable")

    n_emitted = len(stars_sorted)
    if n_emitted < params.min_stars:
        warnings.append(
            f"only {n_emitted} usable stars (< {params.min_stars} recommended for a "
            f"reliable solve) — you may continue, but the astrometric fit will be weak")

    dominant = None
    if template is not None and template.valid:
        dominant = {"theta_deg": float(template.theta_deg),
                    "length_px": float(template.length_px),
                    "tol_deg": float(template.theta_tol_deg)}

    return DetectionResult(
        mode=mode,
        image_dims=dims,
        epoch=meta.get("epoch"),
        exposure_s=meta.get("exposure_s"),
        background_rms=float(background_rms) if background_rms is not None else None,
        dominant_streak=dominant,
        n_stars=n_emitted,
        n_target_candidates=len(candidates),
        warnings=warnings,
        detections=detections,
    )
