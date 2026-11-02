"""
analysis/astrometry/streaks.py  (ported from ssa-toolkit photometry/streaks.py, object mode)
=====================
Phase 3: point/streak classification + the object-track streak template, faint
recovery, and streak repair.

Key idea (object-track only): the star trails are near-parallel and near-equal
length, so a robust consensus (theta_bar, L_bar) from the CONFIDENT streaks lets
us (a) recover faint streaks the primary threshold missed, and (b) repair
streaks that came out short or split by threshold truncation — which is the fix
for the Phase 2 faint-trail length caveat.

Repair rules
------------
  * short streak (angle ~ theta_bar, length < L_bar, NOT edge): re-anchor on the
    flux-weighted centroid and extend symmetrically along theta_bar to L_bar.
  * collinear split fragments (same axis, adjacent, combined span ~ L_bar):
    merge, then reconstruct from the combined centroid.
  * edge-contact streak: the frame clipped it, so DON'T shrink/symmetric-extend.
    Anchor on the interior end and extend OUTWARD by L_bar toward the border
    (the far endpoint may land off-frame). Flagged "edge"; excluded from the
    midpoint plate-solve set downstream, though its interior endpoint stays
    usable for an endpoint-based solve (P4/P5).

Sidereal-track has a single (or few independent) streak(s) — no peer consensus —
so template/repair are skipped there; streaks keep their measured endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .morphology import Source
from .types import MODE_OBJECT


@dataclass
class ClassifyParams:
    elongation_thresh: float = 1.8     # >= -> streak (with min length)
    min_streak_length: float = 6.0     # px, projected major-axis extent
    # consensus / voting
    min_votes: int = 4                 # guardrail: need >=4 confident streaks
    vote_snr_min: float = 20.0         # a streak must be this bright to vote
    theta_tol_deg: float = 6.0
    length_tol_frac: float = 0.15
    extend_min_frac: float = 0.5       # only symmetric-extend a truncated trail to
                                       # the consensus length if its MEASURED span is
                                       # already >= this fraction of L. A tiny aligned
                                       # blob (2-6% of L) stretched to full length is a
                                       # phantom (wrong place/angle); below this it
                                       # keeps its measured (short) endpoints instead.
    length_pctile: float = 85.0        # consensus trail length = this percentile of
                                       # aligned lengths, NOT the median. In object-
                                       # track all trails share ONE length; threshold
                                       # truncation only SHORTENS faint ones, so the
                                       # upper envelope recovers the true length while
                                       # the median is dragged down by truncated
                                       # trails (XTAR: median 25 vs P85 405). Robust
                                       # to over-merges too (they sit above P85).
    # faint recovery
    matched_filter: bool = True
    mf_nsigma: float = 3.0             # lower threshold for the recovery pass
    dedupe_px: float = 6.0             # a recovered centroid this close to an
                                       # existing one is the same source
    # --- trail reconstruction (photometry/trails.py — replaces merge/repair for
    #     object-track: line-cluster fragments by trail-line, matched-filter recover
    #     missed faint trails, walk endpoints). ---
    trail_recon: bool = True           # use reference-guided reconstruction (object)
    perp_tol_px: float = 5.0           # two fragments within this ⟂ offset are the
                                       # same trail-line
    along_split_frac: float = 0.8      # split a line where the along-gap > this·L
                                       # (two collinear trails on one line)
    recover_enabled: bool = True       # (B) matched-filter recovery of missed trails
    recover_snr_min: float = 18.0      # matched-SNR to accept a recovered trail
    recover_min_fill: float = 0.45     # a recovered trail must have flux along at
                                       # least this fraction of the aperture — a
                                       # bright POINT (e.g. the target) integrated
                                       # along θ makes a high-SNR spike but fills
                                       # only a few steps, so this rejects it
    recover_perp_step: float = 3.0     # ⟂ scan step (px)
    walk_enabled: bool = True          # (C) endpoint walk
    walk_win: int = 5                  # along window (px) for the walk
    walk_frac: float = 0.35            # stop the walk when level < this·core
    walk_max_px: int = 600             # safety cap on walk distance
    recon_min_ref: int = 2             # min bright aligned trails to seed a weak
                                       # reference when the strict template is invalid

    # fragment merging (threshold breaks / deblend shatter trails into pieces)
    merge_gap_px: float = 25.0         # max along-axis gap between fragments. Bumped
                                       # from 15: on noisy/lit frames a faint trail
                                       # breaks where it dips below threshold, with
                                       # gaps > 15 px (XTAR S7/S9 gap 19). The L-cap
                                       # in repair prevents this from over-merging two
                                       # distinct end-to-end trails.
    merge_max_len_frac: float = 1.3    # never merge fragments into a streak longer
                                       # than this * consensus L (two distinct
                                       # collinear trails, e.g. MOLNIYA 2x310=630).


@dataclass
class StreakTemplate:
    theta_deg: float
    length_px: float
    n_votes: int
    valid: bool
    theta_tol_deg: float = 6.0
    length_tol_frac: float = 0.15
    recon_diag: Optional[dict] = None   # per-component reconstruction diagnostics


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def classify_sources(sources: list[Source], params: ClassifyParams) -> None:
    """Label each source 'point' or 'streak' in place."""
    for s in sources:
        streak = (s.elongation >= params.elongation_thresh
                  and s.length_px >= params.min_streak_length)
        s.kind = "streak" if streak else "point"


def streaks_of(sources: list[Source]) -> list[Source]:
    return [s for s in sources if s.kind == "streak"]


def _drop_short_misaligned(sources: list[Source], template: StreakTemplate,
                           params: ClassifyParams) -> list[Source]:
    """Remove SHORT streaks whose angle is off the consensus trail direction (real
    trails are all aligned; a short off-axis streak is a spurious noise bit). Long
    misaligned streaks are kept (plane / target). Points pass through untouched."""
    out = []
    for s in sources:
        if s.kind != "streak":
            out.append(s)
            continue
        aligned = abs(_angle_diff(s.orientation_deg, template.theta_deg)) <= template.theta_tol_deg
        if aligned or s.length_px >= 0.5 * template.length_px:
            out.append(s)
    return out


def promote_aligned_streaks(sources: list[Source], template: StreakTemplate,
                            params: ClassifyParams) -> int:
    """Object-track only: reclassify point-labelled sources that are actually
    faint/short TRAILS. A source is promoted to 'streak' when it is elongated
    (>= elongation_thresh) AND its major axis aligns with the consensus trail
    angle — even if it fell below ``min_streak_length`` (a faint star's trail may
    register only a few px above threshold). This stops genuine trails from
    landing in the point pile, where in object mode they would masquerade as
    spurious target candidates. Round points (the satellite, hot pixels) and
    off-axis sources are left untouched. Returns the count promoted."""
    n = 0
    for s in sources:
        if s.kind == "streak":
            continue
        if (s.elongation >= params.elongation_thresh
                and s.length_px >= params.min_streak_length
                and abs(_angle_diff(s.orientation_deg, template.theta_deg))
                <= template.theta_tol_deg):
            s.kind = "streak"
            n += 1
    return n


# ---------------------------------------------------------------------------
# consensus template
# ---------------------------------------------------------------------------

def estimate_template(sources: list[Source], params: ClassifyParams) -> StreakTemplate:
    """Robust (theta_bar, L_bar) from confident, non-edge streaks.

    Voters must be streaks, bright (snr >= vote_snr_min) and not edge-clipped
    (edge streaks are length-truncated by the frame). Deblend-shattered
    fragments are first re-merged along the consensus axis so the length is
    measured on WHOLE trails, not pieces. The target streak, if any, is rejected
    as an angle/length outlier by the sigma-clip.
    """
    invalid = StreakTemplate(theta_deg=0.0, length_px=0.0, n_votes=0, valid=False,
                             theta_tol_deg=params.theta_tol_deg,
                             length_tol_frac=params.length_tol_frac)

    voters = [s for s in streaks_of(sources)
              if s.snr >= params.vote_snr_min and "edge" not in s.flags]
    if len(voters) < params.min_votes:
        return invalid

    theta = _robust_angle(np.array([s.orientation_deg for s in voters]))
    merged = _axis_merge(voters, theta, params)   # whole trails

    # Gate on the ALIGNED (consensus) count, not the raw/merged count: a rogue
    # off-axis streak (plane, or a trailed target) must not pad the vote to reach
    # the guardrail floor, and must not set L_bar.
    aligned = [m for m in merged
               if abs(_angle_diff(m.orientation_deg, theta)) <= params.theta_tol_deg]
    if len(aligned) < params.min_votes:
        return invalid

    # Upper percentile, not median: truncated faint trails only shorten the tail,
    # so P85 tracks the true (full) trail length; over-merges sit above it.
    L = float(np.percentile(np.array([m.length_px for m in aligned]),
                            params.length_pctile))
    return StreakTemplate(theta_deg=float(theta), length_px=float(L),
                          n_votes=len(aligned), valid=True,
                          theta_tol_deg=params.theta_tol_deg,
                          length_tol_frac=params.length_tol_frac)


# ---------------------------------------------------------------------------
# faint recovery (template-gated low-threshold detection)
# ---------------------------------------------------------------------------

def recover_faint_streaks(data_sub, rms, segment_map, existing, template,
                          params, detect_params=None) -> list[Source]:
    """Re-detect at a lower threshold and keep only NEW, template-aligned,
    elongated sources — faint trails the 5-sigma pass missed. Round noise blobs
    (elongation ~ 1) and anything overlapping an existing source are rejected,
    so precision is held by the consensus, not the low threshold."""
    from .detect import DetectParams, extract_sources

    dp = detect_params or DetectParams()
    low = DetectParams(nsigma=params.mf_nsigma, min_area=dp.min_area,
                       connectivity=dp.connectivity, deblend=False,
                       edge_margin=dp.edge_margin, saturation_adu=dp.saturation_adu)
    cand = extract_sources(data_sub, rms, low).sources

    ex = [(s.x, s.y) for s in existing]

    out: list[Source] = []
    for s in cand:
        # Reject a re-detection of an already-found source (same centroid). A
        # dilated-footprint test would wrongly reject a NEW faint trail that
        # merely crosses a long bright streak's footprint, so match on centroid.
        if any(np.hypot(s.x - x, s.y - y) < params.dedupe_px for x, y in ex):
            continue
        if s.elongation < params.elongation_thresh:
            continue                                   # round -> not a trail
        if abs(_angle_diff(s.orientation_deg, template.theta_deg)) > template.theta_tol_deg:
            continue                                   # wrong direction
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------

def repair_streaks(sources: list[Source], template: StreakTemplate,
                   image_shape, params: ClassifyParams) -> list[Source]:
    """Merge split fragments, extend truncated streaks to L_bar, and handle
    edge-contact streaks. Points pass through untouched. Returns a new list."""
    h, w = image_shape
    pts = [s for s in sources if s.kind != "streak"]
    # L-cap the merge so two distinct end-to-end trails aren't fused into one.
    strk = _axis_merge(streaks_of(sources), template.theta_deg, params,
                       max_len=template.length_px * params.merge_max_len_frac)

    for s in strk:
        aligned = abs(_angle_diff(s.orientation_deg, template.theta_deg)) <= template.theta_tol_deg
        is_edge = "edge" in s.flags
        if is_edge and aligned:
            _extend_edge(s, template, (h, w))
            _flag(s, "extended")
        elif (aligned
              and template.length_px * params.extend_min_frac <= s.length_px
              < template.length_px * (1 - template.length_tol_frac)):
            _extend_symmetric(s, template, (h, w))
            _flag(s, "extended")
        else:
            _set_measured_endpoints(s)                # TRUE endpoints (edges-safe)
    return pts + strk


def finalize_endpoints(sources: list[Source]) -> None:
    """Ensure every streak has endpoints set (used when no template/repair ran,
    e.g. sidereal mode). Uses the TRUE measured endpoints so the sidereal target
    streak — the one an edges-export WCS match relies on — is exact, not a
    symmetric reconstruction."""
    for s in streaks_of(sources):
        if s.xs is None:
            _set_measured_endpoints(s)


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

def process_streaks(data_sub, rms, sources, segment_map, mode, image_shape,
                    params: Optional[ClassifyParams] = None,
                    detect_params=None):
    """Classify, and (object-track only) build the template, recover faint
    streaks, and repair. Returns (sources, template|None)."""
    params = params or ClassifyParams()
    classify_sources(sources, params)

    template = None
    if mode == MODE_OBJECT:
        template = estimate_template(sources, params)
        # A weak reference (>= recon_min_ref bright aligned trails) is enough for
        # reconstruction on sparse frames (e.g. 00000724 with ~3 trails) where the
        # strict >=4-vote template is invalid.
        if not template.valid and params.trail_recon:
            import dataclasses as _dc
            template = estimate_template(
                sources, _dc.replace(params, min_votes=params.recon_min_ref))

        if template.valid and params.trail_recon:
            # promote faint/short trails misfiled as points, then reconstruct whole
            # trails (line-cluster + matched-filter recover + endpoint walk).
            promote_aligned_streaks(sources, template, params)
            from .trails import reconstruct_trails
            sources, diag = reconstruct_trails(
                data_sub, rms, sources, template, image_shape, params)
            template.recon_diag = diag
        elif template.valid:
            # legacy merge/repair path (kept for trail_recon=False / regression)
            promote_aligned_streaks(sources, template, params)
            if params.matched_filter:
                recovered = recover_faint_streaks(data_sub, rms, segment_map,
                                                  sources, template, params, detect_params)
                classify_sources(recovered, params)
                sources = sources + recovered
            sources = repair_streaks(sources, template, image_shape, params)
            sources = _drop_short_misaligned(sources, template, params)

    finalize_endpoints(sources)
    sources.sort(key=lambda s: s.flux, reverse=True)
    return sources, template


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _unit(theta_deg):
    t = np.deg2rad(theta_deg)
    return np.cos(t), np.sin(t)


def _set_endpoints(s: Source, length: float) -> None:
    ux, uy = _unit(s.orientation_deg)
    half = length / 2.0
    s.xs, s.ys = s.x - half * ux, s.y - half * uy
    s.xe, s.ye = s.x + half * ux, s.y + half * uy
    s.length_px = length


def _set_measured_endpoints(s: Source) -> None:
    """Set the displayed endpoints to the TRUE measured trail ends (never a
    symmetric reconstruction). Falls back to the reconstruction only if measured
    endpoints are unavailable (shouldn't happen for a measured source)."""
    if s.xs_m is not None:
        s.xs, s.ys, s.xe, s.ye = s.xs_m, s.ys_m, s.xe_m, s.ye_m
        s.length_px = float(np.hypot(s.xe - s.xs, s.ye - s.ys))
    else:
        _set_endpoints(s, s.length_px)


def _flag(s: Source, name: str) -> None:
    if name not in s.flags:
        s.flags.append(name)


def _extend_symmetric(s: Source, template: StreakTemplate, shape) -> None:
    """Threshold-truncated trail: extend symmetrically about the flux centroid to
    the consensus length, ALONG THE TEMPLATE ANGLE (all trails share one angle; the
    source's own short-segment angle is noisy). Endpoints are clamped to the frame;
    if either end would fall off-frame the trail is edge-adjacent, so it's flagged
    'edge' (excluded from the object-track solve)."""
    h, w = shape
    ux, uy = _unit(template.theta_deg)
    half = template.length_px / 2.0
    x1, y1 = s.x - half * ux, s.y - half * uy
    x2, y2 = s.x + half * ux, s.y + half * uy
    (s.xs, s.ys, off1) = _clamp_pt(x1, y1, w, h)
    (s.xe, s.ye, off2) = _clamp_pt(x2, y2, w, h)
    s.orientation_deg = template.theta_deg
    s.length_px = float(np.hypot(s.xe - s.xs, s.ye - s.ys))
    if off1 or off2:
        _flag(s, "edge")


def _clamp_pt(x, y, w, h) -> tuple:
    """Clamp (x, y) into [0, w-1] x [0, h-1]; returns (x, y, was_off_frame)."""
    cx = min(max(x, 0.0), w - 1.0)
    cy = min(max(y, 0.0), h - 1.0)
    return (cx, cy, cx != x or cy != y)


def _extend_edge(s: Source, template: StreakTemplate, shape) -> None:
    """Edge-clipped trail: keep the interior end, extend OUTWARD to L_bar toward
    the border (far end may leave the frame)."""
    h, w = shape
    ux, uy = _unit(s.orientation_deg)
    half = s.length_px / 2.0
    e1 = (s.x - half * ux, s.y - half * uy)
    e2 = (s.x + half * ux, s.y + half * uy)
    d1, d2 = _edge_dist(e1, w, h), _edge_dist(e2, w, h)
    # the end nearer the border is the clipped one; anchor on the interior end
    interior, clipped = (e2, e1) if d1 < d2 else (e1, e2)
    dirx, diry = clipped[0] - interior[0], clipped[1] - interior[1]
    n = np.hypot(dirx, diry) or 1.0
    dirx, diry = dirx / n, diry / n
    far = (interior[0] + template.length_px * dirx,
           interior[1] + template.length_px * diry)
    s.xs, s.ys = interior
    s.xe, s.ye = far
    s.length_px = template.length_px


def _axis_merge(streaks: list[Source], theta_deg: float,
                params: ClassifyParams,
                max_len: Optional[float] = None) -> list[Source]:
    """Greedily merge collinear, adjacent fragments of the same trail along the
    consensus axis. Non-destructive: singletons pass through unchanged, merged
    groups become new Source objects. Handles threshold-broken / deblend-shattered
    trails. ``max_len`` (when given) caps a merged group's span so two distinct
    end-to-end trails are not fused into one over-length streak."""
    if not streaks:
        return []
    ux, uy = _unit(theta_deg)
    items = sorted(streaks, key=lambda s: s.x * ux + s.y * uy)
    groups: list[list[Source]] = []
    for s in items:
        for g in groups:
            if not _same_trail(g[-1], s, ux, uy, params):
                continue
            if max_len is not None and _group_span(g + [s], ux, uy) > max_len:
                continue                       # would over-merge distinct trails
            g.append(s)
            break
        else:
            groups.append([s])
    return [g[0] if len(g) == 1 else _combine_axis(g, theta_deg) for g in groups]


def _group_span(group: list[Source], ux, uy) -> float:
    """Along-axis extent of a fragment group (endpoint projection min→max)."""
    proj = []
    for g in group:
        half = g.length_px / 2.0
        proj.append((g.x - half * ux) * ux + (g.y - half * uy) * uy)
        proj.append((g.x + half * ux) * ux + (g.y + half * uy) * uy)
    return max(proj) - min(proj)


def _same_trail(a: Source, b: Source, ux, uy, params) -> bool:
    dx, dy = b.x - a.x, b.y - a.y
    perp = abs(-dx * uy + dy * ux)
    along = abs(dx * ux + dy * uy)
    if perp > max(3.0, 0.6 * (a.width_px + b.width_px)):
        return False
    gap = along - (a.length_px + b.length_px) / 2.0   # <=0 means overlapping
    return gap <= params.merge_gap_px


def _combine_axis(group: list[Source], theta_deg: float) -> Source:
    import dataclasses

    ux, uy = _unit(theta_deg)
    fluxes = np.array([g.flux for g in group], dtype=float)
    cx = float(np.average([g.x for g in group], weights=fluxes))
    cy = float(np.average([g.y for g in group], weights=fluxes))

    # extreme fragment endpoints along the axis define the merged span
    ends = []
    for g in group:
        half = g.length_px / 2.0
        ends.append((g.x - half * ux, g.y - half * uy))
        ends.append((g.x + half * ux, g.y + half * uy))
    proj = [(px * ux + py * uy, (px, py)) for px, py in ends]
    pmin, pmax = min(proj), max(proj)
    length = pmax[0] - pmin[0]

    base = max(group, key=lambda g: g.flux)
    return dataclasses.replace(
        base, x=cx, y=cy, flux=float(fluxes.sum()), orientation_deg=theta_deg,
        length_px=length, xs=pmin[1][0], ys=pmin[1][1], xe=pmax[1][0], ye=pmax[1][1],
        # the combined span IS the true merged endpoint set (edges-safe)
        xs_m=pmin[1][0], ys_m=pmin[1][1], xe_m=pmax[1][0], ye_m=pmax[1][1],
        flags=list(dict.fromkeys(base.flags + ["merged"])),
    )


def _edge_dist(pt, w, h) -> float:
    x, y = pt
    return float(min(x, y, w - 1 - x, h - 1 - y))


def _angle_diff(a, b) -> float:
    """Smallest signed difference of two axis angles (mod 180), in degrees."""
    d = (a - b + 90.0) % 180.0 - 90.0
    return d


def _robust_angle(angles) -> float:
    """Median axis angle, handling the +-90 wrap by working relative to the
    strongest reference then folding."""
    ref = angles[0]
    rel = np.array([_angle_diff(a, ref) for a in angles])
    return float(ref + np.median(rel))


def _sigma_clipped_median(x, sigma=2.5, iters=3) -> float:
    x = np.asarray(x, dtype=float)
    for _ in range(iters):
        m, s = np.median(x), np.std(x)
        if s == 0:
            break
        keep = np.abs(x - m) <= sigma * s
        if keep.all() or keep.sum() < 2:
            break
        x = x[keep]
    return float(np.median(x))
