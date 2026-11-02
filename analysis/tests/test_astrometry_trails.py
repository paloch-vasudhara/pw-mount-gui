# ruff: noqa: E402, E501, E702, E741, B905
"""
Trail-reconstruction tests (photometry/trails.py) — the object-track
line-clustering (A), matched-filter recovery (B), and endpoint walk (C) that
replace the fragment-merge/repair path.
"""

from __future__ import annotations

import numpy as np
import pytest

from analysis.astrometry.engine import detect_image
from analysis.astrometry.morphology import Source
from analysis.astrometry.streaks import ClassifyParams
from analysis.astrometry.trails import (
    cluster_lines,
    trail_from_cluster,
)
from analysis.astrometry.types import MODE_OBJECT


def _frag(x, y, length, angle, flux=1e4):
    """A streak fragment with true endpoints set (centroid ± L/2 along angle)."""
    th = np.deg2rad(angle); ux, uy = np.cos(th), np.sin(th)
    h = length / 2.0
    return Source(
        label=1, x=x, y=y, flux=flux, peak=flux / 10, area=length * 2,
        a_sigma=length / 3.5, b_sigma=1.0, elongation=max(2.0, length / 2),
        orientation_deg=angle, length_px=length, width_px=2.0, fwhm=2.0,
        snr=40.0, bbox=(int(x - length), int(y - 2), int(x + length), int(y + 2)),
        kind="streak", flags=[],
        xs_m=x - h * ux, ys_m=y - h * uy, xe_m=x + h * ux, ye_m=y + h * uy)


# ── A: line clustering ───────────────────────────────────────────────────────

def test_cluster_lines_reunites_fragments_across_gaps():
    """Fragments on ONE trail-line (same perp offset) merge into one trail even
    with a large along-gap that the old adjacency merge would have missed."""
    theta, L = 0.0, 100.0
    # three collinear fragments on y=100, spread along x with big gaps
    frags = [_frag(20, 100, 12, 0.0), _frag(60, 100, 12, 0.0), _frag(95, 100, 12, 0.0)]
    clusters = cluster_lines(frags, theta, L, ClassifyParams())
    assert len(clusters) == 1
    tr = trail_from_cluster(clusters[0], theta, L, None)
    assert tr.length_px == pytest.approx(89, abs=6)     # ~14→101 span
    assert abs(tr.y - 100) < 1.0


def test_cluster_lines_splits_two_trails_on_one_line():
    """Two trails collinear on the same infinite line but separated along-axis by
    > L are split (they can't be one trail)."""
    theta, L = 0.0, 60.0
    frags = [_frag(30, 100, 20, 0.0), _frag(300, 100, 20, 0.0)]   # ~270 apart
    clusters = cluster_lines(frags, theta, L, ClassifyParams())
    assert len(clusters) == 2


def test_cluster_lines_keeps_parallel_trails_separate():
    theta, L = 0.0, 100.0
    frags = [_frag(50, 100, 40, 0.0), _frag(50, 140, 40, 0.0)]    # 40px perp apart
    clusters = cluster_lines(frags, theta, L, ClassifyParams())
    assert len(clusters) == 2


# ── B + C via the full reconstruction on a synthetic frame ───────────────────

def _streak_img(shape, cx, cy, length, angle, amp, sigma=1.3):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    ang = np.deg2rad(angle)
    out = np.zeros(shape)
    for t in np.linspace(-length / 2, length / 2, int(length) * 2 + 1):
        px, py = cx + t * np.cos(ang), cy + t * np.sin(ang)
        out += amp * np.exp(-(((xx - px) ** 2 + (yy - py) ** 2) / (2 * sigma ** 2)))
    return out


def test_reconstruction_recovers_a_gapped_trail_as_one():
    """A single trail broken by a mid-length brightness DROP is emitted as ONE
    whole trail (line-clustering + walk), not two fragments."""
    shape = (240, 300)
    rng = np.random.default_rng(0)
    data = np.zeros(shape)
    # 5 clean reference trails (angle 20, length 40)
    for i in range(5):
        data += _streak_img(shape, 60 + 40 * i, 60, 40, 20.0, amp=60)
    # a 6th trail with a GAP in the middle (two bright ends, dark centre)
    data += _streak_img(shape, 150, 170, 14, 20.0, amp=60)      # one end
    data += _streak_img(shape, 168, 177, 14, 20.0, amp=60)      # other end (gap between)
    data += rng.normal(0, 4.0, shape)

    res = detect_image(data, MODE_OBJECT)
    strk = res.stars
    # the gapped trail region should yield ONE streak spanning both ends, not two
    near = [s for s in strk if 140 < s.midpoint()[0] < 180 and 160 < s.midpoint()[1] < 190]
    assert len(near) == 1, f"expected 1 reconstructed trail, got {len(near)}"


def test_recovery_does_not_fabricate_trail_on_a_point():
    """A bright POINT integrated along θ spikes the matched filter but fills only a
    few steps — the fill-gate must reject it (no phantom trail on the target)."""
    shape = (240, 300)
    rng = np.random.default_rng(1)
    data = np.zeros(shape)
    for i in range(6):
        data += _streak_img(shape, 50 + 35 * i, 70, 30, 20.0, amp=60)
    # a bright round point (the "target") away from the trails
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    data += 80 * np.exp(-(((xx - 150) ** 2 + (yy - 180) ** 2) / (2 * 1.8 ** 2)))
    data += rng.normal(0, 4.0, shape)

    res = detect_image(data, MODE_OBJECT)
    # no emitted STREAK should sit on the point at (150,180)
    for s in res.stars:
        mx, my = s.midpoint()
        assert not (abs(mx - 150) < 8 and abs(my - 180) < 8), \
            "matched-filter fabricated a trail on the point"
