# ruff: noqa: E402, E501, E702, E741, B905
"""
Phase 3 tests — photometry.streaks: classification, the object-track streak
template, faint recovery, and streak repair (extend / merge / edge).
"""

from __future__ import annotations

import numpy as np
import pytest

from analysis.astrometry.detect import DetectParams, extract_sources
from analysis.astrometry.morphology import Source
from analysis.astrometry.streaks import (
    ClassifyParams,
    StreakTemplate,
    _angle_diff,
    _axis_merge,
    classify_sources,
    estimate_template,
    process_streaks,
    recover_faint_streaks,
    repair_streaks,
    streaks_of,
)
from analysis.astrometry.types import MODE_OBJECT, MODE_SIDEREAL

# ── image helpers ────────────────────────────────────────────────────────────

def _point(shape, cx, cy, amp, sigma=1.8):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    return amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2)))


def _streak(shape, cx, cy, length, angle_deg, amp, sigma=1.3):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    ang = np.deg2rad(angle_deg)
    out = np.zeros(shape)
    for t in np.linspace(-length / 2, length / 2, int(length) * 2 + 1):
        px, py = cx + t * np.cos(ang), cy + t * np.sin(ang)
        out += amp * np.exp(-(((xx - px) ** 2 + (yy - py) ** 2) / (2 * sigma ** 2)))
    return out


def _parallel_field(shape=(300, 380), angle=20.0, length=26, n=6, amp_k=10,
                    noise=5.0, target_point=True, seed=0):
    rng = np.random.default_rng(seed)
    data = np.zeros(shape)
    positions = []
    for _ in range(n):
        cx = rng.uniform(40, shape[1] - 40)
        cy = rng.uniform(40, shape[0] - 40)
        data += _streak(shape, cx, cy, length, angle, amp=noise * amp_k)
        positions.append((cx, cy))
    if target_point:
        data += _point(shape, shape[1] / 2, shape[0] / 2, amp=noise * 12)
    data += rng.normal(0, noise, shape)
    return data, np.full(shape, noise), positions


# ── fabricated Source helper (for repair/merge/edge geometry) ────────────────

def _mk_streak(x, y, length, angle, flux=1e4, snr=40.0, flags=None, width=2.0):
    return Source(
        label=1, x=x, y=y, flux=flux, peak=flux / 10, area=length * 2,
        a_sigma=length / 3.5, b_sigma=width / 2, elongation=max(2.0, length / width),
        orientation_deg=angle, length_px=length, width_px=width, fwhm=width,
        snr=snr, bbox=(int(x - length), int(y - 2), int(x + length), int(y + 2)),
        kind="streak", flags=list(flags or []),
    )


# ── classification ───────────────────────────────────────────────────────────

def test_classify_points_and_streaks():
    shape = (240, 300)
    noise = 5.0
    data = _point(shape, 60, 60, amp=noise * 12) + _point(shape, 200, 90, amp=noise * 12)
    data += _streak(shape, 150, 180, 28, 25.0, amp=noise * 9)
    data += np.random.default_rng(1).normal(0, noise, shape)
    # deblend off here: it shatters a single trail into pieces (a real effect
    # handled by _axis_merge downstream); classification alone is the SUT.
    res = extract_sources(data, np.full(shape, noise), DetectParams(deblend=False))
    classify_sources(res.sources, ClassifyParams())
    kinds = sorted(s.kind for s in res.sources)
    assert kinds.count("streak") == 1
    assert kinds.count("point") == 2


# ── template ─────────────────────────────────────────────────────────────────

def test_template_recovers_angle_and_length():
    data, rms, _ = _parallel_field(angle=22.0, length=26, n=6, seed=2)
    res = extract_sources(data, rms)
    classify_sources(res.sources, ClassifyParams())
    tpl = estimate_template(res.sources, ClassifyParams())
    assert tpl.valid
    assert tpl.n_votes >= 4
    assert abs(_angle_diff(tpl.theta_deg, 22.0)) < 3.0
    # consensus length ~ the whole-trail projected extent (a length-26 trail with
    # PSF wings spans ~30 px above threshold); fragments are re-merged first.
    assert tpl.length_px == pytest.approx(30, abs=6)


def test_template_guardrail_too_few_streaks():
    data, rms, _ = _parallel_field(n=2, seed=3)
    res = extract_sources(data, rms)
    classify_sources(res.sources, ClassifyParams())
    tpl = estimate_template(res.sources, ClassifyParams(min_votes=4))
    assert not tpl.valid


def test_angle_diff_wraps_at_ninety():
    assert _angle_diff(89.0, -89.0) == pytest.approx(-2.0)
    assert _angle_diff(-89.0, 89.0) == pytest.approx(2.0)
    assert _angle_diff(10.0, 8.0) == pytest.approx(2.0)


def test_guardrail_gates_on_aligned_count_not_padding():
    # 3 genuine aligned trails + 1 rogue off-axis streak must NOT pass the >=4
    # guardrail — the outlier can't pad the vote (B1).
    src = [_mk_streak(50, 40 + 30 * i, length=26, angle=20.0) for i in range(3)]
    src.append(_mk_streak(200, 200, length=48, angle=70.0))
    tpl = estimate_template(src, ClassifyParams(min_votes=4))
    assert not tpl.valid
    # add a 4th genuine aligned trail -> now valid, and only the 4 aligned count
    src.append(_mk_streak(50, 160, length=26, angle=20.0))
    tpl2 = estimate_template(src, ClassifyParams(min_votes=4))
    assert tpl2.valid and tpl2.n_votes == 4
    assert abs(_angle_diff(tpl2.theta_deg, 20.0)) < 2.0


def test_axis_merge_keeps_distinct_parallel_trails_separate():
    # two parallel trails offset in perp (different y) must NOT merge
    a = _mk_streak(100, 100, length=20, angle=0.0)
    b = _mk_streak(100, 120, length=20, angle=0.0)   # 20 px perp offset
    out = _axis_merge([a, b], 0.0, ClassifyParams())
    assert len(out) == 2
    # a genuinely fragmented trail (collinear, small gap) DOES merge
    f1 = _mk_streak(100, 100, length=10, angle=0.0)
    f2 = _mk_streak(112, 100, length=10, angle=0.0)   # ~2 px gap edge-to-edge
    merged = _axis_merge([f1, f2], 0.0, ClassifyParams())
    assert len(merged) == 1 and "merged" in merged[0].flags


def test_template_robust_to_outlier_streak():
    # 6 aligned streaks + 1 rogue (different angle & length) must not shift theta/L
    src = [_mk_streak(50 + 30 * i, 60, length=26, angle=20.0) for i in range(6)]
    src.append(_mk_streak(200, 200, length=48, angle=70.0))   # e.g. a plane / target
    tpl = estimate_template(src, ClassifyParams())
    assert tpl.valid
    assert abs(_angle_diff(tpl.theta_deg, 20.0)) < 2.0
    assert tpl.length_px == pytest.approx(26, abs=2)


# ── repair: extend truncated ─────────────────────────────────────────────────

def test_repair_extends_truncated_streak():
    tpl = StreakTemplate(theta_deg=0.0, length_px=30.0, n_votes=6, valid=True)
    # decoys are PARALLEL-OFFSET (different y), so they don't merge with each other
    full = [_mk_streak(80, 40 + 30 * i, length=30, angle=0.0) for i in range(5)]
    short = _mk_streak(200, 220, length=18, angle=0.0)   # truncated (>=0.5*L so it
                                                          # is a real trail, not a
                                                          # blob → eligible to extend)
    out = repair_streaks(full + [short], tpl, (400, 400), ClassifyParams())
    repaired = [s for s in out if abs(s.x - 200) < 1 and abs(s.y - 220) < 1][0]
    assert repaired.length_px == pytest.approx(30.0, abs=1e-6)
    # symmetric about the original centroid (200,220), along theta=0
    assert repaired.xs == pytest.approx(185.0) and repaired.xe == pytest.approx(215.0)
    assert repaired.ys == pytest.approx(220.0) and repaired.ye == pytest.approx(220.0)


# ── repair: merge split fragments ────────────────────────────────────────────

def test_repair_merges_collinear_fragments():
    tpl = StreakTemplate(theta_deg=0.0, length_px=30.0, n_votes=6, valid=True)
    full = [_mk_streak(80, 40 + 30 * i, length=30, angle=0.0) for i in range(5)]
    # two short collinear pieces of one broken trail (centroids 8px apart on x)
    f1 = _mk_streak(246, 250, length=10, angle=0.0, flux=4000)
    f2 = _mk_streak(254, 250, length=10, angle=0.0, flux=4000)
    out = repair_streaks(full + [f1, f2], tpl, (400, 400), ClassifyParams())
    near = [s for s in streaks_of(out) if abs(s.y - 250) < 2 and 240 < s.x < 260]
    assert len(near) == 1, "fragments not merged into one"
    m = near[0]
    assert "merged" in m.flags
    assert m.length_px == pytest.approx(30.0, abs=1e-6)
    assert m.x == pytest.approx(250.0, abs=0.5)   # flux-weighted centroid


# ── repair: edge streak extended outward, not shrunk ─────────────────────────

def test_edge_streak_extends_outward():
    tpl = StreakTemplate(theta_deg=0.0, length_px=30.0, n_votes=6, valid=True)
    full = [_mk_streak(80, 40 + 30 * i, length=30, angle=0.0) for i in range(5)]
    # streak hard against the left border: centroid near x=5, measured length small
    edge = _mk_streak(5, 250, length=10, angle=0.0, flags=["edge"])
    out = repair_streaks(full + [edge], tpl, (400, 400), ClassifyParams())
    e = [s for s in streaks_of(out) if "edge" in s.flags][0]
    assert e.length_px == pytest.approx(30.0, abs=1e-6)
    # interior end is the right one (x=10); far end pushed OUTWARD past the border
    interior_x, far_x = max(e.xs, e.xe), min(e.xs, e.xe)
    assert interior_x == pytest.approx(10.0, abs=1e-6)
    assert far_x == pytest.approx(-20.0, abs=1e-6)   # 10 - 30, off-frame (valid)


# ── faint recovery ───────────────────────────────────────────────────────────

def test_faint_recovery_finds_subthreshold_streak():
    shape = (280, 340)
    noise = 5.0
    data, rms, _ = _parallel_field(shape=shape, angle=18.0, length=26, n=6,
                                   amp_k=11, noise=noise, target_point=False, seed=5)
    # a faint, sub-5-sigma aligned streak the primary pass will miss
    fx, fy = 70.0, 230.0
    data += _streak(shape, fx, fy, 26, 18.0, amp=noise * 4.2)
    # a faint ROUND blob that must NOT be recovered (rejected on shape, not brightness)
    data += _point(shape, 300, 40, amp=noise * 4.2)

    res = extract_sources(data, rms, DetectParams(nsigma=5.0))
    classify_sources(res.sources, ClassifyParams())
    tpl = estimate_template(res.sources, ClassifyParams())
    assert tpl.valid

    # primary pass missed the faint streak
    assert not any(np.hypot(s.x - fx, s.y - fy) < 6 for s in res.sources)

    recovered = recover_faint_streaks(data, rms, res.segment_map, res.sources,
                                      tpl, ClassifyParams())
    assert any(np.hypot(s.x - fx, s.y - fy) < 8 for s in recovered), "faint streak not recovered"
    assert not any(np.hypot(s.x - 300, s.y - 40) < 8 for s in recovered), "round blob wrongly recovered"


# ── sidereal skips template ──────────────────────────────────────────────────

def test_sidereal_skips_template_but_sets_endpoints():
    shape = (240, 300)
    noise = 5.0
    # sidereal: many points + one target streak
    data = np.zeros(shape)
    for cx, cy in [(60, 60), (200, 80), (120, 180), (250, 200)]:
        data += _point(shape, cx, cy, amp=noise * 12)
    data += _streak(shape, 150, 120, 30, 40.0, amp=noise * 10)
    data += np.random.default_rng(6).normal(0, noise, shape)
    rms = np.full(shape, noise)

    res = extract_sources(data, rms)
    sources, tpl = process_streaks(data, rms, res.sources, res.segment_map,
                                   MODE_SIDEREAL, shape, ClassifyParams())
    assert tpl is None
    strk = streaks_of(sources)
    assert strk and all(s.xs is not None for s in strk)   # endpoints still set


def test_process_object_end_to_end():
    data, rms, _ = _parallel_field(angle=15.0, length=24, n=6, seed=7)
    res = extract_sources(data, rms)
    sources, tpl = process_streaks(data, rms, res.sources, res.segment_map,
                                   MODE_OBJECT, data.shape, ClassifyParams())
    assert tpl is not None and tpl.valid
    strk = streaks_of(sources)
    assert strk and all(s.xs is not None and s.xe is not None for s in strk)
    # streaks near the consensus length after repair
    lengths = [s.length_px for s in strk if "edge" not in s.flags]
    assert np.median(lengths) == pytest.approx(tpl.length_px, abs=6)


# ── tuning #2 round-2: template length percentile, L-cap merge, true endpoints ──

def test_template_length_uses_upper_percentile_not_median():
    """Truncated faint trails only shorten the length tail; the consensus must
    track the FULL length (upper percentile), not the median."""
    # 6 truncated (L=12) + 4 full (L=100) aligned, parallel (distinct perp rows)
    src = [_mk_streak(150, y, 12, 0.0) for y in (20, 45, 70, 95, 120, 145)]
    src += [_mk_streak(150, y, 100, 0.0) for y in (170, 200, 230, 260)]
    tpl = estimate_template(src, ClassifyParams())
    assert tpl.valid
    assert tpl.length_px > 60          # recovered the full length, not the ~12 median
    assert np.median([s.length_px for s in src]) < 30   # median would have failed


def test_axis_merge_lcap_prevents_over_merge():
    """Two full-length collinear trails end-to-end must NOT fuse when an L-cap is
    given (they'd exceed it), but do merge with no cap (legacy fragment behaviour)."""
    a = _mk_streak(100, 100, 100, 0.0)
    b = _mk_streak(200, 100, 100, 0.0)     # touching end-to-end, combined span ~200
    assert len(_axis_merge([a, b], 0.0, ClassifyParams())) == 1          # no cap
    assert len(_axis_merge([a, b], 0.0, ClassifyParams(), max_len=130)) == 2  # capped


def test_measured_endpoints_preserved_edges_safe():
    from analysis.astrometry.streaks import _set_measured_endpoints, finalize_endpoints
    s = _mk_streak(100, 100, 50, 0.0)
    s.xs_m, s.ys_m, s.xe_m, s.ye_m = 75.0, 101.0, 125.0, 99.0
    _set_measured_endpoints(s)
    assert (s.xs, s.ys, s.xe, s.ye) == (75.0, 101.0, 125.0, 99.0)
    # finalize (sidereal path) must use the TRUE measured ends, not a symmetric recon
    s2 = _mk_streak(100, 100, 50, 0.0)
    s2.xs_m, s2.ys_m, s2.xe_m, s2.ye_m = 70.0, 100.0, 130.0, 100.0
    s2.xs = None
    finalize_endpoints([s2])
    assert (s2.xs, s2.xe) == (70.0, 130.0)


# ── tuning #2 round-3: extension gate / template-angle / clamp / misaligned drop ─

def test_extension_gated_by_measured_fraction():
    """A tiny aligned blob (< extend_min_frac·L) keeps its measured length; a
    genuinely-truncated trail (>= the fraction) is extended to L."""
    tpl = StreakTemplate(theta_deg=0.0, length_px=100.0, n_votes=6, valid=True)
    full = [_mk_streak(80, 40 + 40 * i, 100, 0.0) for i in range(5)]
    blob = _mk_streak(200, 200, 10, 0.0)    # frac 0.10 -> NOT extended
    trunc = _mk_streak(320, 200, 60, 0.0)   # frac 0.60 -> extended
    out = repair_streaks(full + [blob, trunc], tpl, (500, 500), ClassifyParams())
    b = [s for s in out if abs(s.x - 200) < 1][0]
    t = [s for s in out if abs(s.x - 320) < 1][0]
    assert b.length_px < 20 and "extended" not in b.flags
    assert t.length_px == pytest.approx(100.0) and "extended" in t.flags


def test_extension_uses_template_angle_and_clamps_edge():
    tpl = StreakTemplate(theta_deg=0.0, length_px=100.0, n_votes=6, valid=True)
    full = [_mk_streak(80, 40 + 40 * i, 100, 0.0) for i in range(5)]
    near = _mk_streak(470, 200, 60, 3.0)    # aligned, eligible, near right edge
    out = repair_streaks(full + [near], tpl, (500, 500), ClassifyParams())
    s = [s for s in out if abs(s.x - 470) < 1][0]
    assert s.orientation_deg == pytest.approx(0.0)     # template angle, not 3
    assert "edge" in s.flags                            # extension ran off-frame
    assert max(s.xs, s.xe) <= 499.0                     # clamped in-frame


def test_drop_short_misaligned_streaks():
    from analysis.astrometry.streaks import _drop_short_misaligned
    tpl = StreakTemplate(theta_deg=0.0, length_px=100.0, n_votes=6, valid=True,
                         theta_tol_deg=6.0)
    aligned_short = _mk_streak(100, 100, 20, 2.0)     # aligned short  -> keep
    mis_short = _mk_streak(200, 100, 20, 40.0)        # misaligned short -> DROP
    mis_long = _mk_streak(300, 100, 80, 40.0)         # misaligned long -> keep (plane)
    pt = _mk_streak(400, 100, 20, 0.0); pt.kind = "point"
    out = _drop_short_misaligned([aligned_short, mis_short, mis_long, pt], tpl,
                                 ClassifyParams())
    xs = [round(s.x) for s in out]
    assert 200 not in xs                 # misaligned short dropped
    assert 100 in xs and 300 in xs and 400 in xs
