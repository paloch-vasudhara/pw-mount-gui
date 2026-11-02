# ruff: noqa: E402, E501, E702, E741, B905
"""
Phase 4 tests — role assignment, target selection, one-extraction/re-label, and
click-to-snap (photometry.select + photometry.engine.DetectionEngine).
Synthetic frames with known geometry (real-frame tuning is a separate session).
"""

from __future__ import annotations

import json

import numpy as np

from analysis.astrometry.engine import DetectionEngine, detect_image
from analysis.astrometry.select import SelectParams
from analysis.astrometry.types import (
    MODE_OBJECT,
    MODE_SIDEREAL,
    ROLE_TARGET,
    SHAPE_POINT,
    SHAPE_STREAK,
)

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


def _object_frame(shape=(320, 400), angle=20.0, length=26, n_stars=8,
                  noise=5.0, tgt=(200.0, 160.0), seed=0):
    """Object-track: n parallel star STREAKS + one bright POINT target."""
    rng = np.random.default_rng(seed)
    data = np.zeros(shape)
    centers = []
    # lay stars on a jittered 2-D grid so no two land collinear within the axis-
    # merge gap (which would legitimately fuse them into one long streak)
    cols, rows = 4, 3
    gx = np.linspace(55, shape[1] - 55, cols)
    gy = np.linspace(55, shape[0] - 55, rows)
    cells = [(x, y) for y in gy for x in gx]
    rng.shuffle(cells)
    for cx, cy in cells[:n_stars]:
        cx += rng.uniform(-8, 8); cy += rng.uniform(-8, 8)
        data += _streak(shape, cx, cy, length, angle, amp=noise * 10)
        centers.append((cx, cy))
    data += _point(shape, tgt[0], tgt[1], amp=noise * 14)
    data += rng.normal(0, noise, shape)
    return data, centers, tgt


def _sidereal_frame(shape=(320, 400), n_stars=14, noise=5.0,
                    tgt=(210.0, 150.0), tgt_len=30, tgt_angle=35.0, seed=1):
    """Sidereal-track: n star POINTS + one satellite STREAK target."""
    rng = np.random.default_rng(seed)
    data = np.zeros(shape)
    for _ in range(n_stars):
        cx = rng.uniform(40, shape[1] - 40)
        cy = rng.uniform(40, shape[0] - 40)
        data += _point(shape, cx, cy, amp=noise * 11)
    data += _streak(shape, tgt[0], tgt[1], tgt_len, tgt_angle, amp=noise * 12)
    data += rng.normal(0, noise, shape)
    return data, tgt


# ── role assignment ──────────────────────────────────────────────────────────

def test_object_target_is_point_stars_are_streaks():
    data, centers, tgt = _object_frame(n_stars=8, seed=2)
    res = detect_image(data, MODE_OBJECT)

    assert res.target is not None
    assert res.target.shape == SHAPE_POINT
    assert res.target.role == ROLE_TARGET
    # the target is the bright compact point near the injected position
    assert np.hypot(res.target.x - tgt[0], res.target.y - tgt[1]) < 2.0

    stars = res.stars
    assert len(stars) >= 6
    # every star is a streak and exposes a usable midpoint for the plate solve
    assert all(s.shape == SHAPE_STREAK for s in stars)
    assert all(s.midpoint() is not None for s in stars)
    assert res.n_stars == len(stars)


def test_sidereal_target_is_streak_stars_are_points():
    data, tgt = _sidereal_frame(n_stars=14, seed=3)
    res = detect_image(data, MODE_SIDEREAL)

    assert res.target is not None
    assert res.target.shape == SHAPE_STREAK
    mid = res.target.midpoint()
    assert mid is not None and np.hypot(mid[0] - tgt[0], mid[1] - tgt[1]) < 3.0

    stars = res.stars
    assert len(stars) >= 10
    assert all(s.shape == SHAPE_POINT for s in stars)


# ── one-extraction / instant re-label ────────────────────────────────────────

def test_mode_toggle_relabels_from_same_cache():
    # a frame with BOTH a point and star streaks so each mode has a valid target
    data, centers, tgt = _object_frame(n_stars=8, seed=4)
    eng = DetectionEngine(data)
    n_base = len(eng._base_sources)

    r_obj = eng.label(MODE_OBJECT)
    r_sid = eng.label(MODE_SIDEREAL)

    # the expensive primary extraction ran exactly once, cache untouched
    assert eng.n_extractions == 1
    assert len(eng._base_sources) == n_base

    # ...yet the two modes label the SAME sources differently
    assert r_obj.mode == MODE_OBJECT and r_sid.mode == MODE_SIDEREAL
    assert r_obj.target.shape == SHAPE_POINT     # object: sat is the point
    assert r_sid.target.shape == SHAPE_STREAK    # sidereal: sat is a streak


def test_relabel_does_not_mutate_cache_geometry():
    data, _, _ = _object_frame(seed=5)
    eng = DetectionEngine(data)
    before = [(s.x, s.y, s.xs) for s in eng._base_sources]
    eng.label(MODE_OBJECT)   # repair/extend edits endpoints on its own copy
    after = [(s.x, s.y, s.xs) for s in eng._base_sources]
    assert before == after


# ── endpoint / midpoint fidelity ─────────────────────────────────────────────

def test_star_midpoint_rms_under_half_px():
    # "stars = streak midpoints": the midpoint feeding the solve must sit on the
    # true star position. Clean, well-separated parallel streaks.
    data, centers, _ = _object_frame(n_stars=8, length=24, noise=4.0, seed=6)
    res = detect_image(data, MODE_OBJECT)

    errs = []
    for s in res.stars:
        mx, my = s.midpoint()
        d = min(np.hypot(mx - cx, my - cy) for cx, cy in centers)
        errs.append(d)
    assert len(errs) >= 6
    rms = float(np.sqrt(np.mean(np.square(errs))))
    assert rms < 0.5, f"star midpoint RMS {rms:.3f}px exceeds 0.5px"


def test_center_bias_prefers_central_target_when_comparable():
    # two point candidates: a slightly brighter OFF-centre one and a slightly
    # dimmer CENTRAL one, plus star streaks. center_bias=0 -> brightest (off-centre);
    # a strong center_bias -> the central one wins.
    shape = (320, 400)
    data, centers, _ = _object_frame(shape=shape, n_stars=6, seed=11, tgt=(360.0, 60.0))
    # inject a central, slightly dimmer point on top of the off-centre target
    data = data + _point(shape, 200.0, 160.0, amp=4.0 * 12.0)   # noise=5 default -> ~*12
    off = detect_image(data, MODE_OBJECT, select_params=SelectParams(center_bias=0.0))
    cen = detect_image(data, MODE_OBJECT, select_params=SelectParams(center_bias=0.8))
    # off-centre (brighter) target far from centre; central target near (200,160)
    assert np.hypot(off.target.x - 360.0, off.target.y - 60.0) < 4.0
    assert np.hypot(cen.target.x - 200.0, cen.target.y - 160.0) < 4.0


# ── click-to-snap ────────────────────────────────────────────────────────────

def test_measure_at_snaps_within_2px():
    data, centers, tgt = _object_frame(seed=7)
    eng = DetectionEngine(data)
    # click a few px off the true target point -> snaps onto it
    det = eng.measure_at(tgt[0] + 3.0, tgt[1] - 2.5)
    assert "snapped" in det.flags
    assert np.hypot(det.x - tgt[0], det.y - tgt[1]) < 2.0


def test_measure_at_empty_region_returns_manual_point():
    data, centers, tgt = _object_frame(seed=8)
    eng = DetectionEngine(data)
    # a blank corner far from any source
    det = eng.measure_at(12.0, 12.0)
    assert det.shape == SHAPE_POINT
    assert "manual" in det.flags
    # nothing to snap to -> lands at (or very near) the requested click
    assert np.hypot(det.x - 12.0, det.y - 12.0) < 3.0


# ── downsample-for-detection + full-res refine ───────────────────────────────

def test_downsampled_detection_refines_target_to_full_res():
    # force downsampling (detect_max_px small -> f>1) and confirm the target
    # centroid is refined back to full-res accuracy, not left at 1/f granularity.
    data, centers, tgt = _object_frame(shape=(480, 600), n_stars=8, seed=12)
    res = detect_image(data, MODE_OBJECT, detect_max_px=200)   # f = ceil(600/200)=3
    assert tuple(res.image_dims) == (600, 480)                 # reported at full-res
    assert any("1/3 scale" in w for w in res.warnings)
    assert res.target is not None and res.target.shape == SHAPE_POINT
    # refined to well under one full-res pixel of slop (a pure 1/3 map would be ±3)
    assert np.hypot(res.target.x - tgt[0], res.target.y - tgt[1]) < 1.5
    # stars scaled to full-res too (spread across the full frame, not the 1/3 image)
    assert res.stars and max(s.midpoint()[0] for s in res.stars) > 300


# ── soft min-star warning ────────────────────────────────────────────────────

def test_too_few_stars_soft_warning_but_continuable():
    data, centers, tgt = _object_frame(n_stars=4, seed=9)   # < min_stars (10)
    res = detect_image(data, MODE_OBJECT)
    assert any("usable stars" in w for w in res.warnings)
    # SOFT: results are still produced (target + the few stars), not blocked
    assert res.target is not None
    assert res.n_stars == len(res.stars) >= 1


# ── contract / serialisation ─────────────────────────────────────────────────

def test_result_is_json_serialisable():
    data, _, _ = _object_frame(seed=10)
    res = detect_image(data, MODE_OBJECT)
    d = res.to_dict()
    s = json.dumps(d)
    back = json.loads(s)
    assert back["mode"] == MODE_OBJECT
    assert isinstance(back["detections"], list) and back["detections"]
    assert back["image_dims"] == [400, 320]   # (width, height) as a list


# ── tuning #2: deblend-off + round-target + edge invalidation (real-frame bugs) ─
# These lock in the fixes for XTAR-EUR object-mode detection (see
# docs/astrometry_engine_logs.md tuning #2): deblending shatters star trails into
# spurious points, an elongated artefact could win the target, and edge-clipped
# trails polluted the solve set.

from analysis.astrometry.morphology import Source
from analysis.astrometry.select import assign_roles
from analysis.astrometry.streaks import (
    ClassifyParams,
    StreakTemplate,
    promote_aligned_streaks,
)


def _src(x, y, flux, elong, length, angle=0.0, kind="point", flags=None,
         snr=50.0, width=3.0):
    return Source(
        label=1, x=x, y=y, flux=flux, peak=flux, area=max(length * width, 1.0),
        a_sigma=length / 4.0, b_sigma=width / 4.0, elongation=elong,
        orientation_deg=angle, length_px=length, width_px=width, fwhm=2.0,
        snr=snr, bbox=(int(x - 2), int(y - 2), int(x + 2), int(y + 2)),
        flags=list(flags or []), kind=kind)


def test_object_mode_disables_deblend_sidereal_keeps_it():
    data, _, _ = _object_frame(seed=3)
    assert DetectionEngine(data, mode=MODE_OBJECT)._dp.deblend is False
    assert DetectionEngine(data, mode=MODE_SIDEREAL)._dp.deblend is True


def test_bright_elongated_point_not_chosen_as_target():
    """A bright ELONGATED point (plane / mark / short off-axis trail) must not
    outrank the fainter ROUND satellite for the object-track target."""
    dims = (400, 320)
    sat = _src(200, 160, 1.0e5, 1.2, 3, kind="point")          # round, central
    bright_elong = _src(50, 60, 3.0e5, 4.0, 8, angle=10, kind="point")
    stars = [_src(x, y, 2.0e5, 5.0, 26, angle=20, kind="streak")
             for x, y in [(80, 80), (300, 80), (80, 240), (300, 240), (150, 150)]]
    res = assign_roles([sat, bright_elong] + stars, MODE_OBJECT, None, dims, {},
                       5.0, SelectParams())
    tgt = next(d for d in res.detections if d.role == ROLE_TARGET)
    assert (round(tgt.x), round(tgt.y)) == (200, 160)


def test_edge_streak_excluded_from_solve_set_object():
    dims = (400, 320)
    sat = _src(200, 160, 1.0e5, 1.2, 3, kind="point")
    good = [_src(x, y, 2.0e5, 5.0, 26, angle=20, kind="streak")
            for x, y in [(80, 80), (300, 80), (80, 240), (300, 240)]]
    edge = _src(392, 160, 2.0e5, 5.0, 26, angle=20, kind="streak", flags=["edge"])
    res = assign_roles([sat, edge] + good, MODE_OBJECT, None, dims, {}, 5.0,
                       SelectParams())
    star_streaks = [d for d in res.detections
                    if d.role != ROLE_TARGET and d.shape == SHAPE_STREAK]
    assert star_streaks and all("edge" not in (d.flags or []) for d in star_streaks)
    assert any("edge" in w for w in res.warnings)


def test_promote_aligned_short_trail_only():
    tmpl = StreakTemplate(theta_deg=20.0, length_px=26, n_votes=6, valid=True,
                          theta_tol_deg=6.0)
    aligned_short = _src(100, 100, 1e4, 3.0, 8, angle=21, kind="point")  # promote
                                       # (len>=min_streak_length so it's a real short
                                       # trail, not a sub-6px blob)
    round_pt = _src(120, 120, 1e4, 1.2, 3, angle=0, kind="point")        # stays
    off_axis = _src(140, 140, 1e4, 4.0, 8, angle=80, kind="point")       # stays
    n = promote_aligned_streaks([aligned_short, round_pt, off_axis], tmpl,
                                ClassifyParams())
    assert n == 1
    assert aligned_short.kind == "streak"
    assert round_pt.kind == "point" and off_axis.kind == "point"


def test_object_mode_emits_no_alternate_points():
    """Object-track stars are streaks; alternate ROUND points must not clutter the
    overlay. The candidate COUNT is still reported."""
    dims = (400, 320)
    sat = _src(200, 160, 1.0e5, 1.2, 3, kind="point")
    extra_pts = [_src(x, y, 8.0e4, 1.3, 3, kind="point")
                 for x, y in [(60, 60), (340, 60), (60, 260)]]   # round non-targets
    stars = [_src(x, y, 2.0e5, 5.0, 26, angle=20, kind="streak")
             for x, y in [(120, 90), (300, 90), (120, 230), (300, 230)]]
    res = assign_roles([sat] + extra_pts + stars, MODE_OBJECT, None, dims, {},
                       5.0, SelectParams())
    assert not any("alternate" in (d.flags or []) for d in res.detections)
    emitted_points = [d for d in res.detections if d.shape == SHAPE_POINT]
    assert len(emitted_points) == 1 and emitted_points[0].role == ROLE_TARGET
    assert res.n_target_candidates >= 4          # count still surfaced
