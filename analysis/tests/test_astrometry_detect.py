# ruff: noqa: E402, E501, E702, E741, B905
"""
Phase 2 tests — photometry.detect + photometry.morphology.

Detection recall / centroid accuracy, point-vs-streak elongation separation,
deblending, false-positive rate on blank noise, QA flags, and the length/width
projected extents.  Synthetic frames with known truth.
"""

from __future__ import annotations

import numpy as np
import pytest

from analysis.astrometry.background import BackgroundParams, reduce_frame
from analysis.astrometry.detect import DetectParams, ExtractionResult, extract_sources


def _point(shape, cx, cy, amp, sigma=1.8):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    return amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2)))


def _streak(shape, cx, cy, length, angle_deg, amp, sigma=1.3):
    """A streak = many PSFs smeared along a line of given length/angle."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    ang = np.deg2rad(angle_deg)
    out = np.zeros(shape)
    for t in np.linspace(-length / 2, length / 2, int(length) * 2 + 1):
        px, py = cx + t * np.cos(ang), cy + t * np.sin(ang)
        out += amp * np.exp(-(((xx - px) ** 2 + (yy - py) ** 2) / (2 * sigma ** 2)))
    return out


def _match(truth_xy, sources, tol=2.0):
    """Return (n_matched, residuals) matching each truth to nearest source."""
    resid = []
    matched = 0
    for (tx, ty) in truth_xy:
        best = min(sources, key=lambda s: (s.x - tx) ** 2 + (s.y - ty) ** 2,
                   default=None)
        if best is None:
            continue
        d = np.hypot(best.x - tx, best.y - ty)
        if d <= tol:
            matched += 1
            resid.append(d)
    return matched, np.array(resid)


# ── recall + centroid accuracy ───────────────────────────────────────────────

def test_detection_recall_and_centroid():
    rng = np.random.default_rng(1)
    shape = (256, 320)
    noise = 8.0
    data = np.zeros(shape)
    truth = []
    for _ in range(25):
        cx, cy = rng.uniform(15, shape[1] - 15), rng.uniform(15, shape[0] - 15)
        data += _point(shape, cx, cy, amp=noise * 12)   # comfortably > 5 sigma
        truth.append((cx, cy))
    data += rng.normal(0, noise, shape)
    rms = np.full(shape, noise)

    res = extract_sources(data, rms, DetectParams(nsigma=5.0))
    matched, resid = _match(truth, res.sources)

    assert matched >= 0.9 * len(truth), f"recall {matched}/{len(truth)}"
    assert float(np.sqrt(np.mean(resid ** 2))) < 0.3, "centroid RMS >= 0.3 px"


# ── point vs streak separation ───────────────────────────────────────────────

def test_elongation_separates_points_and_streaks():
    shape = (240, 300)
    noise = 6.0
    data = np.zeros(shape)
    pts = [(60, 60), (200, 80), (120, 180)]
    strk = [(240, 200, 24, 20.0), (80, 220, 30, 20.0)]
    for cx, cy in pts:
        data += _point(shape, cx, cy, amp=noise * 12)
    for cx, cy, L, a in strk:
        data += _streak(shape, cx, cy, L, a, amp=noise * 9)
    data += np.random.default_rng(2).normal(0, noise, shape)
    rms = np.full(shape, noise)

    res = extract_sources(data, rms)
    point_e = [s.elongation for s in res.sources if s.length_px < 8]
    streak_e = [s.elongation for s in res.sources if s.length_px > 15]

    assert point_e and streak_e
    assert max(point_e) < 1.6, f"a 'point' is too elongated: {max(point_e):.2f}"
    assert min(streak_e) > 2.5, f"a 'streak' not elongated enough: {min(streak_e):.2f}"
    assert max(point_e) < min(streak_e), "no clean gap between points and streaks"


# ── deblending ───────────────────────────────────────────────────────────────

def test_deblend_splits_close_pair():
    shape = (120, 120)
    noise = 5.0
    # close + bright enough that the bridge between them stays above threshold,
    # so they form ONE parent segment that deblending must split.
    data = (_point(shape, 57, 60, amp=noise * 20, sigma=2.2)
            + _point(shape, 63, 60, amp=noise * 20, sigma=2.2))
    data += np.random.default_rng(3).normal(0, noise, shape)
    rms = np.full(shape, noise)

    res = extract_sources(data, rms, DetectParams(deblend=True))
    assert res.n_sources == 2
    assert all(s.blended for s in res.sources)
    assert all("blended" in s.flags for s in res.sources)

    # without deblending the pair merges into one blob
    res1 = extract_sources(data, rms, DetectParams(deblend=False))
    assert res1.n_sources == 1


def test_well_separated_sources_not_blended():
    # negative case: distinct parents must NOT be flagged as blended
    shape = (140, 200)
    noise = 5.0
    data = _point(shape, 40, 70, amp=noise * 14) + _point(shape, 160, 70, amp=noise * 14)
    data += np.random.default_rng(31).normal(0, noise, shape)
    rms = np.full(shape, noise)
    res = extract_sources(data, rms, DetectParams(deblend=True))
    assert res.n_sources == 2
    assert not any(s.blended for s in res.sources)
    assert not any("blended" in s.flags for s in res.sources)


# ── false positives on blank noise ───────────────────────────────────────────

def test_false_positives_on_blank():
    shape = (256, 256)
    noise = 10.0
    data = np.random.default_rng(4).normal(0, noise, shape)
    rms = np.full(shape, noise)
    res = extract_sources(data, rms, DetectParams(nsigma=5.0, min_area=5))
    # 5-sigma + 5-connected-pixels on pure Gaussian noise -> essentially none
    assert res.n_sources <= 2, f"too many false positives: {res.n_sources}"


# ── QA flags ─────────────────────────────────────────────────────────────────

def test_edge_and_saturation_flags():
    shape = (160, 160)
    noise = 6.0
    data = _point(shape, 4, 80, amp=noise * 12)          # near left edge
    data += _point(shape, 90, 90, amp=5000, sigma=2.0)   # bright -> saturated
    data += np.random.default_rng(5).normal(0, noise, shape)
    rms = np.full(shape, noise)

    res = extract_sources(data, rms, DetectParams(edge_margin=6, saturation_adu=3000))
    by_x = sorted(res.sources, key=lambda s: s.x)
    assert "edge" in by_x[0].flags
    assert any("saturated" in s.flags for s in res.sources)


def test_saturation_judged_on_raw_frame():
    # With a big sky pedestal, the subtracted peak sits below the raw ADU
    # saturation level; passing raw= must still flag it (and not flag it otherwise).
    shape = (120, 120)
    noise, sky = 6.0, 5000.0
    src = _point(shape, 60, 60, amp=2000, sigma=2.0)   # raw peak ~7000
    raw = sky + src + np.random.default_rng(51).normal(0, noise, shape)
    data_sub = src + np.random.default_rng(52).normal(0, noise, shape)  # peak ~2000
    rms = np.full(shape, noise)

    # judged on subtracted peak (~2000) -> NOT flagged at 6500
    r_nb = extract_sources(data_sub, rms, DetectParams(saturation_adu=6500))
    assert not any("saturated" in s.flags for s in r_nb.sources)
    # judged on raw peak (~7000) -> flagged
    r_b = extract_sources(data_sub, rms, DetectParams(saturation_adu=6500), raw=raw)
    assert any("saturated" in s.flags for s in r_b.sources)


def test_varying_rms_map_local_threshold():
    # RMS rises steeply on the right half; an identical-amplitude source there
    # should have a lower SNR than the same source on the quiet left half.
    shape = (120, 200)
    base_noise = 4.0
    rms = np.empty(shape)
    rms[:, :100] = base_noise
    rms[:, 100:] = base_noise * 4
    data = _point(shape, 50, 60, amp=120) + _point(shape, 150, 60, amp=120)
    rng = np.random.default_rng(61)
    data = data + rng.normal(0, rms)   # per-pixel noise from the map
    res = extract_sources(data, rms, DetectParams(nsigma=5.0))
    left = min(res.sources, key=lambda s: s.x)
    right = max(res.sources, key=lambda s: s.x)
    assert left.snr > right.snr, "local RMS not reflected in per-source SNR"


# ── length / width extents ───────────────────────────────────────────────────

def test_length_width_extents():
    shape = (200, 260)
    noise = 5.0
    data = _streak(shape, 130, 100, length=30, angle_deg=0.0, amp=noise * 10)
    data += np.random.default_rng(6).normal(0, noise, shape)
    rms = np.full(shape, noise)

    res = extract_sources(data, rms)
    s = max(res.sources, key=lambda s: s.length_px)
    assert s.length_px == pytest.approx(30, abs=6)   # recovers injected length
    assert s.length_px > 3 * s.width_px              # clearly a streak
    assert abs(s.orientation_deg) < 8                # ~horizontal


def test_snr_increases_with_brightness():
    shape = (140, 140)
    noise = 6.0
    data = _point(shape, 40, 70, amp=noise * 8) + _point(shape, 100, 70, amp=noise * 25)
    data += np.random.default_rng(7).normal(0, noise, shape)
    rms = np.full(shape, noise)
    res = extract_sources(data, rms)
    faint = min(res.sources, key=lambda s: s.peak)
    bright = max(res.sources, key=lambda s: s.peak)
    assert bright.snr > faint.snr


# ── integration with Phase 1 ─────────────────────────────────────────────────

def test_pipeline_reduce_then_extract():
    shape = (220, 280)
    rng = np.random.default_rng(8)
    sky, noise = 1000.0, 7.0
    data = np.full(shape, sky)
    data += 40.0 * np.sin(np.arange(shape[1]) * 0.7)[np.newaxis, :]   # stripe
    truth = []
    for _ in range(12):
        cx, cy = rng.uniform(20, shape[1] - 20), rng.uniform(20, shape[0] - 20)
        data += _point(shape, cx, cy, amp=noise * 12)
        truth.append((cx, cy))
    data += rng.normal(0, noise, shape)

    red = reduce_frame(data, BackgroundParams(destripe=True))
    res = extract_sources(red.data_sub, red.rms)
    matched, _ = _match(truth, res.sources)
    assert matched >= 0.9 * len(truth)
    assert isinstance(res, ExtractionResult)


def test_no_sources_returns_empty():
    shape = (80, 80)
    data = np.random.default_rng(9).normal(0, 5.0, shape)
    res = extract_sources(data, np.full(shape, 5.0), DetectParams(nsigma=8.0))
    assert res.n_sources == 0
    assert res.segment_map.shape == shape
