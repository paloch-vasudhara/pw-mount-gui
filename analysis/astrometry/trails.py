"""
analysis/astrometry/trails.py  (ported from ssa-toolkit photometry/trails.py)
====================
Reference-guided trail reconstruction for OBJECT-TRACK frames (tuning #2 round-4).

Replaces the fragile fragment-adjacency merge + symmetric-extend with a model-based
reconstruction that exploits the strong object-track prior: every star trail is a
PARALLEL line of the SAME length L at the consensus angle θ. Three components,
each independently instrumented (see ``reconstruct_trails`` -> diag):

  A. line-clustering  — group fragments by their PERPENDICULAR offset (the line
     they lie on). Fragments of one trail share a line regardless of the gaps
     between them, so a trail shattered into pieces rejoins into one trail. A line
     spanning >> L (two collinear trails) is split along-axis.
  B. matched-filter recovery — with (θ, L) fixed, scan candidate trail-lines and
     accept those whose oriented-aperture matched-SNR clears a threshold. Recovers
     faint trails the per-pixel threshold missed entirely (the human eye does this).
  C. endpoint walk — from each trail's flux extent, walk outward along θ until the
     running luminosity drops, giving true sub-pixel endpoints (edges-safe) and
     edge-clip detection.

Sidereal-track has no parallel consensus, so this module is object-track only; the
caller keeps the measured-endpoint path for sidereal.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from .morphology import Source
from .streaks import StreakTemplate, _flag, _unit

# ---------------------------------------------------------------------------
# geometry helpers (axis frame: along = θ direction, perp = ⟂ to it)
# ---------------------------------------------------------------------------

def _axis(theta_deg):
    ux, uy = _unit(theta_deg)
    return ux, uy, -uy, ux            # ux,uy (along) ; px,py (perp)


def _perp_of(x, y, px, py):
    return x * px + y * py


def _along_of(x, y, ux, uy):
    return x * ux + y * uy


def _endpoints_along(x, y, ux, uy):
    """(along_start, along_end) span of a source's TRUE measured endpoints; falls
    back to centroid±L/2 if measured endpoints are absent."""
    if x is None:
        return None
    return _along_of(x, y, ux, uy)


# ---------------------------------------------------------------------------
# oriented matched filter (local-background subtracted)
# ---------------------------------------------------------------------------

def _sample_nn(data, xs, ys):
    """Nearest-neighbour sample; out-of-frame -> nan."""
    h, w = data.shape
    xi = np.round(xs).astype(int)
    yi = np.round(ys).astype(int)
    ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
    out = np.full(xs.shape, np.nan, dtype=float)
    out[ok] = data[yi[ok], xi[ok]]
    return out


def oriented_profile(data, cx, cy, theta_deg, L, half_w=2.0, bg_off=8.0):
    """Along-axis flux profile (core minus local side-band background) for a
    length-L aperture centred at (cx,cy). Returns (t, sig) where t is the along
    offset (px) and sig the background-subtracted core profile (nan where OOB)."""
    ux, uy, px, py = _axis(theta_deg)
    t = np.arange(-L / 2.0, L / 2.0 + 1.0)

    def band(s_values):
        acc = np.full((t.size, len(s_values)), np.nan)
        for j, s in enumerate(s_values):
            xs = cx + t * ux + s * px
            ys = cy + t * uy + s * py
            acc[:, j] = _sample_nn(data, xs, ys)
        return np.nanmean(acc, axis=1)

    with np.errstate(invalid="ignore"):
        core = band(np.arange(-half_w, half_w + 1.0))
        bg = band([-bg_off, -bg_off - 1.0, bg_off, bg_off + 1.0])
    return t, core - bg, bg


def matched_snr(data, cx, cy, theta_deg, L, half_w=2.0, bg_off=8.0) -> float:
    """Integrated matched SNR of an oriented aperture: Σsig / (√N · local_noise)."""
    _, sig, bg = oriented_profile(data, cx, cy, theta_deg, L, half_w, bg_off)
    m = np.isfinite(sig)
    n = int(m.sum())
    if n < max(8, 0.3 * sig.size):
        return 0.0
    noise = float(np.nanstd(bg))
    if not np.isfinite(noise) or noise <= 0:
        return 0.0
    return float(np.nansum(sig[m]) / (np.sqrt(n) * noise))


# ---------------------------------------------------------------------------
# A — line clustering
# ---------------------------------------------------------------------------

def cluster_lines(streaks: list[Source], theta_deg: float, L: float,
                  params) -> list[list[Source]]:
    """Group fragments by perpendicular offset (same trail-line), then split a line
    that spans much more than L (two collinear trails)."""
    if not streaks:
        return []
    ux, uy, px, py = _axis(theta_deg)
    items = sorted(streaks, key=lambda s: _perp_of(s.x, s.y, px, py))
    clusters: list[list[Source]] = [[items[0]]]
    last_perp = _perp_of(items[0].x, items[0].y, px, py)
    for s in items[1:]:
        perp = _perp_of(s.x, s.y, px, py)
        if perp - last_perp <= params.perp_tol_px:
            clusters[-1].append(s)
        else:
            clusters.append([s])
        last_perp = perp

    out: list[list[Source]] = []
    for c in clusters:
        out.extend(_along_split(c, theta_deg, L, params))
    return out


def _along_split(cluster: list[Source], theta_deg: float, L: float,
                 params) -> list[list[Source]]:
    """Split a line-cluster wherever the along-axis gap between consecutive
    fragments exceeds ~0.8·L (a single trail can't bridge that)."""
    ux, uy, px, py = _axis(theta_deg)
    frags = sorted(cluster, key=lambda s: _along_of(s.x, s.y, ux, uy))
    groups: list[list[Source]] = [[frags[0]]]
    max_gap = params.along_split_frac * L
    for s in frags[1:]:
        prev = groups[-1][-1]
        gap = _along_of(s.x, s.y, ux, uy) - _along_of(prev.x, prev.y, ux, uy)
        if gap > max_gap:
            groups.append([s])
        else:
            groups[-1].append(s)
    return groups


def trail_from_cluster(cluster: list[Source], theta_deg: float, L: float,
                       data_sub) -> Source:
    """Build one trail Source from a line-cluster: centre = flux-weighted, endpoints
    = along-extent of the fragments' true endpoints, angle = template θ."""
    ux, uy, px, py = _axis(theta_deg)
    fluxes = np.array([max(s.flux, 1.0) for s in cluster], float)
    perp = float(np.average([_perp_of(s.x, s.y, px, py) for s in cluster], weights=fluxes))

    ends = []
    for s in cluster:
        for (ex, ey) in _fragment_ends(s):
            ends.append(_along_of(ex, ey, ux, uy))
    a_lo, a_hi = min(ends), max(ends)
    a_c = 0.5 * (a_lo + a_hi)
    length = a_hi - a_lo

    cx = a_c * ux + perp * px
    cy = a_c * uy + perp * py
    xs = a_lo * ux + perp * px
    ys = a_lo * uy + perp * py
    xe = a_hi * ux + perp * px
    ye = a_hi * uy + perp * py

    base = max(cluster, key=lambda s: s.flux)
    flags = ["merged"] if len(cluster) > 1 else []
    tr = dataclasses.replace(
        base, x=cx, y=cy, orientation_deg=theta_deg, length_px=float(length),
        flux=float(fluxes.sum()), kind="streak",
        xs=xs, ys=ys, xe=xe, ye=ye, xs_m=xs, ys_m=ys, xe_m=xe, ye_m=ye,
        flags=list(dict.fromkeys(list(base.flags) + flags)),
    )
    return tr


def _fragment_ends(s: Source):
    if s.xs_m is not None:
        return [(s.xs_m, s.ys_m), (s.xe_m, s.ye_m)]
    return [(s.x, s.y)]


# ---------------------------------------------------------------------------
# B — matched-filter recovery of missed trails
# ---------------------------------------------------------------------------

def _line_strip(data, perp, a_vals, ux, uy, px, py, s_values):
    """Mean-over-perpendicular-band flux sampled ALONG a whole trail-line at the
    perp offset, for every along position in ``a_vals`` at once (vectorised)."""
    acc = np.zeros((len(s_values), a_vals.size))
    for j, s in enumerate(s_values):
        xs = a_vals * ux + (perp + s) * px
        ys = a_vals * uy + (perp + s) * py
        acc[j] = _sample_nn(data, xs, ys)
    with np.errstate(invalid="ignore"):
        return np.nanmean(acc, axis=0)


def recover_trails(data_sub, theta_deg: float, L: float,
                   existing: list[Source], shape, params) -> list[Source]:
    """Scan candidate trail-lines (perpendicular offsets). Each line is sampled ONCE
    as a strip; a length-L sliding window (cumsum) finds the best trail position and
    its matched-SNR. Lines that clear ``recover_snr_min`` and don't overlap an
    existing trail are accepted."""
    h, w = shape
    ux, uy, px, py = _axis(theta_deg)
    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    perps = [_perp_of(x, y, px, py) for x, y in corners]
    alongs = [_along_of(x, y, ux, uy) for x, y in corners]
    p_lo, p_hi = min(perps), max(perps)
    a_lo, a_hi = min(alongs), max(alongs)
    a_vals = np.arange(a_lo, a_hi + 1.0)
    Li = int(round(L))
    if a_vals.size <= Li:
        return []

    ex_perp = [_perp_of(s.x, s.y, px, py) for s in existing]
    core_s = np.arange(-2.0, 3.0)
    bg_s = np.array([-8.0, -9.0, 8.0, 9.0])

    hits: list[tuple] = []   # (perp, snr, a_center)
    for perp in np.arange(p_lo, p_hi + 1.0, params.recover_perp_step):
        if any(abs(perp - ep) < params.perp_tol_px * 1.5 for ep in ex_perp):
            continue
        core = _line_strip(data_sub, perp, a_vals, ux, uy, px, py, core_s)
        bg = _line_strip(data_sub, perp, a_vals, ux, uy, px, py, bg_s)
        sig = np.nan_to_num(core - bg)
        valid = np.isfinite(core) & np.isfinite(bg)
        noise = float(np.nanstd(bg[valid])) if valid.any() else 0.0
        if noise <= 0:
            continue
        # sliding length-L window sum via cumulative sum
        cs = np.concatenate([[0.0], np.cumsum(sig)])
        wsum = cs[Li:] - cs[:-Li]                       # window sums, len N-Li
        vc = np.concatenate([[0.0], np.cumsum(valid.astype(float))])
        wvalid = vc[Li:] - vc[:-Li]
        snr = np.where(wvalid >= 0.5 * Li, wsum / (np.sqrt(Li) * noise), 0.0)
        k = int(np.argmax(snr))
        if snr[k] >= params.recover_snr_min:
            # fill factor: fraction of the winning window that actually carries
            # flux. A real trail fills most of the aperture; a bright POINT (target)
            # integrated along the axis spikes the SNR but fills only a few steps.
            win = sig[k:k + Li]
            fill = float(np.mean(win > 0.5 * noise))
            if fill >= params.recover_min_fill:
                a_center = a_vals[k] + L / 2.0
                hits.append((float(perp), float(snr[k]), float(a_center)))

    # collapse adjacent perp offsets that fired for the same trail -> keep the max
    hits.sort(key=lambda z: z[0])
    merged: list[tuple] = []
    for hpe, hsnr, hac in hits:
        if merged and hpe - merged[-1][0] < params.perp_tol_px * 2.0:
            if hsnr > merged[-1][1]:
                merged[-1] = (hpe, hsnr, hac)
        else:
            merged.append((hpe, hsnr, hac))

    out = []
    for hpe, hsnr, hac in merged:
        cx = hac * ux + hpe * px
        cy = hac * uy + hpe * py
        out.append(_make_trail(cx, cy, theta_deg, L, hsnr, flags=["recovered"]))
    return out


def _make_trail(cx, cy, theta_deg, L, snr, flags) -> Source:
    ux, uy, px, py = _axis(theta_deg)
    half = L / 2.0
    xs, ys = cx - half * ux, cy - half * uy
    xe, ye = cx + half * ux, cy + half * uy
    return Source(
        label=-1, x=float(cx), y=float(cy), flux=float(max(snr, 0.0)),
        peak=0.0, area=float(L), a_sigma=L / 4.0, b_sigma=1.0,
        elongation=float(max(L, 1.0)), orientation_deg=float(theta_deg),
        length_px=float(L), width_px=2.0, fwhm=2.0, snr=float(snr),
        bbox=(0, 0, 0, 0), kind="streak", flags=list(flags),
        xs=xs, ys=ys, xe=xe, ye=ye, xs_m=xs, ys_m=ys, xe_m=xe, ye_m=ye,
    )


# ---------------------------------------------------------------------------
# C — endpoint walk
# ---------------------------------------------------------------------------

def walk_endpoints(data_sub, trail: Source, theta_deg: float, shape, params) -> None:
    """Refine a trail's endpoints by walking outward from its current extent along
    ±θ until the running (windowed) luminosity drops below a fraction of the trail's
    core level. Clamp to frame; flag 'edge' if the walk hits the border."""
    h, w = shape
    ux, uy, px, py = _axis(theta_deg)
    a_c = _along_of(trail.x, trail.y, ux, uy)
    perp = _perp_of(trail.x, trail.y, px, py)

    # reference core level = median of the interior along-profile
    _, sig, _ = oriented_profile(data_sub, trail.x, trail.y, theta_deg,
                                 max(trail.length_px, 8.0))
    ref = np.nanmedian(sig[np.isfinite(sig)]) if np.isfinite(sig).any() else 0.0
    thresh = params.walk_frac * ref
    win = params.walk_win

    def edge_along(direction):
        a = a_c + direction * (trail.length_px / 2.0)
        steps = 0
        hit_border = False
        while steps < params.walk_max_px:
            xa = (a + direction * win) * ux + perp * px
            ya = (a + direction * win) * uy + perp * py
            if not (0 <= xa < w and 0 <= ya < h):
                hit_border = True
                break
            _, s, _ = oriented_profile(data_sub, xa, ya, theta_deg, float(2 * win))
            level = np.nanmean(s) if np.isfinite(s).any() else -1e9
            if ref <= 0 or level < thresh:
                break
            a += direction * win
            steps += win
        return a, hit_border

    a_lo, hit_lo = edge_along(-1.0)
    a_hi, hit_hi = edge_along(+1.0)
    trail.xs = float(np.clip(a_lo * ux + perp * px, 0, w - 1))
    trail.ys = float(np.clip(a_lo * uy + perp * py, 0, h - 1))
    trail.xe = float(np.clip(a_hi * ux + perp * px, 0, w - 1))
    trail.ye = float(np.clip(a_hi * uy + perp * py, 0, h - 1))
    trail.xs_m, trail.ys_m = trail.xs, trail.ys
    trail.xe_m, trail.ye_m = trail.xe, trail.ye
    trail.length_px = float(np.hypot(trail.xe - trail.xs, trail.ye - trail.ys))
    if hit_lo or hit_hi:
        _flag(trail, "edge")


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

def reconstruct_trails(data_sub, rms, sources: list[Source],
                       template: StreakTemplate, shape, params):
    """Object-track trail reconstruction (A + B + C). Returns (sources_out, diag).
    Points pass through untouched; streaks are rebuilt into whole trails."""
    theta, L = template.theta_deg, template.length_px
    streaks = [s for s in sources if s.kind == "streak"]
    points = [s for s in sources if s.kind != "streak"]
    diag = {"in_streaks": len(streaks), "L": round(L, 1), "theta": round(theta, 1)}

    # A — line clustering
    clusters = cluster_lines(streaks, theta, L, params)
    trails = [trail_from_cluster(c, theta, L, data_sub) for c in clusters]
    diag["A_clusters"] = len(clusters)

    # B — matched-filter recovery
    recovered = []
    if params.recover_enabled:
        recovered = recover_trails(data_sub, theta, L, trails, shape, params)
        trails += recovered
    diag["B_recovered"] = len(recovered)

    # C — endpoint walk (refine all)
    if params.walk_enabled:
        for tr in trails:
            walk_endpoints(data_sub, tr, theta, shape, params)

    diag["out_trails"] = len(trails)
    return trails + points, diag
