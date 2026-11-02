# ruff: noqa: E402, E501, E702, E741, B905
"""
Phase 1 tests — photometry.background: background estimation, RMS map, and
column de-striping.  Synthetic frames with known truth (real-frame tuning is a
separate session once sample images land).
"""

from __future__ import annotations

import numpy as np
import pytest

from analysis.astrometry.background import (
    BackgroundParams,
    BackgroundResult,
    column_pattern_amplitude,
    reduce_frame,
)

RNG = np.random.default_rng(42)


def _gaussian(shape, cx, cy, amp, sigma=2.0):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    return amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2)))


def _frame(shape=(200, 240), sky=1000.0, noise=8.0,
           gradient=0.0, stripe_amp=0.0, n_sources=6, src_amp=400.0, seed=0):
    """Synthesise a frame with optional gradient, per-column stripe, and sources.
    Returns (image, source_positions, stripe_offsets_truth)."""
    rng = np.random.default_rng(seed)
    h, w = shape
    img = np.full(shape, sky, dtype=np.float64)

    if gradient:
        yy, xx = np.mgrid[0:h, 0:w]
        img += gradient * (xx / w) + 0.5 * gradient * (yy / h)

    stripe = np.zeros(w)
    if stripe_amp:
        stripe = stripe_amp * np.sin(np.arange(w) * 0.7) + rng.normal(0, stripe_amp, w)
        stripe -= stripe.mean()
        img += stripe[np.newaxis, :]

    positions = []
    for _ in range(n_sources):
        cx, cy = rng.uniform(20, w - 20), rng.uniform(20, h - 20)
        img += _gaussian(shape, cx, cy, src_amp)
        positions.append((cx, cy))

    img += rng.normal(0, noise, shape)
    return img, positions, stripe


# ── de-striping ──────────────────────────────────────────────────────────────

def test_destripe_reduces_column_pattern_80pct():
    img, _, _ = _frame(stripe_amp=60.0, seed=1)
    before = column_pattern_amplitude(img)

    res = reduce_frame(img, BackgroundParams(destripe=True))
    after = column_pattern_amplitude(res.data_sub)

    assert before > 0
    assert after < 0.2 * before, f"stripe reduction <80%: {before:.2f} -> {after:.2f}"


def test_destripe_offsets_track_truth():
    img, _, stripe = _frame(stripe_amp=50.0, n_sources=4, seed=2)
    res = reduce_frame(img, BackgroundParams(destripe=True))
    # recovered offsets should correlate strongly with the injected stripe
    off = res.destripe_offsets
    assert off is not None and off.shape[0] == img.shape[1]
    corr = np.corrcoef(off, stripe)[0, 1]
    assert corr > 0.9, f"offset/truth correlation {corr:.3f} too low"


def test_destripe_rows_axis():
    # inject a per-ROW stripe and remove it along the rows axis
    shape = (200, 240)
    base, _, _ = _frame(shape=shape, stripe_amp=0.0, n_sources=3, noise=6.0, seed=9)
    rng = np.random.default_rng(9)
    row_off = 50.0 * np.sin(np.arange(shape[0]) * 0.6) + rng.normal(0, 50.0, shape[0])
    row_off -= row_off.mean()
    img = base + row_off[:, np.newaxis]

    res = reduce_frame(img, BackgroundParams(destripe=True, destripe_axis="rows"))
    assert res.destripe_offsets.shape[0] == shape[0]
    corr = np.corrcoef(res.destripe_offsets, row_off)[0, 1]
    assert corr > 0.9, f"row offset/truth correlation {corr:.3f} too low"


def test_toggle_off_is_passthrough():
    img, _, _ = _frame(stripe_amp=40.0, seed=3)
    res = reduce_frame(img, BackgroundParams(destripe=False))
    assert res.destriped is False
    assert res.destripe_offsets is None
    # data_sub is exactly data - background (no column modification)
    assert np.allclose(res.data_sub, (img - res.background).astype(np.float32), atol=1e-3)


# ── background / gradient ────────────────────────────────────────────────────

def test_background_removes_gradient():
    img, _, _ = _frame(gradient=300.0, stripe_amp=0.0, n_sources=0, noise=5.0, seed=4)
    res = reduce_frame(img, BackgroundParams(destripe=False))
    # background-subtracted frame is flat around zero
    assert abs(float(np.median(res.data_sub))) < 3.0
    # recovered background matches the true field (sky+gradient) within noise
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    truth = 1000.0 + 300.0 * (xx / img.shape[1]) + 150.0 * (yy / img.shape[0])
    assert np.median(np.abs(res.background - truth)) < 6.0


def test_rms_map_sane():
    img, _, _ = _frame(noise=10.0, stripe_amp=30.0, seed=5)
    res = reduce_frame(img)
    assert np.all(np.isfinite(res.rms))
    assert np.all(res.rms > 0)
    # RMS tracks the injected noise (loose bounds; mesh + stripe inflate slightly)
    assert 5.0 < float(np.median(res.rms)) < 25.0


def test_rms_floor_lifts_degenerate_regions():
    # A frame with a normal-noise sky next to a perfectly flat (zero-noise) block,
    # as seen on sky-subtracted products / unexposed borders. Without the floor the
    # flat block's mesh RMS collapses toward 0 and the nsigma*rms threshold there
    # goes to ~0; the floor must lift it toward the sky RMS.
    shape = (200, 240)
    img, _, _ = _frame(shape=shape, stripe_amp=0.0, n_sources=0, noise=8.0, seed=21)
    img[:, :80] = 0.0  # dead/unexposed block -> zero local scatter

    res = reduce_frame(img, BackgroundParams(destripe=False))
    sky_rms = float(np.median(res.rms[:, 120:]))     # normal region
    block_rms = float(np.median(res.rms[:, :60]))    # floored region
    assert block_rms >= 0.25 * sky_rms, f"floor not applied: {block_rms:.2f} vs sky {sky_rms:.2f}"

    # ...and disabling the floor leaves the collapsed region near zero
    res0 = reduce_frame(img, BackgroundParams(destripe=False, rms_floor_frac=0.0))
    assert float(np.median(res0.rms[:, :60])) < block_rms


def test_rms_tracks_noise_level():
    # median RMS should scale with the injected noise, not sit at a fixed value
    lo = reduce_frame(_frame(noise=4.0, stripe_amp=0.0, n_sources=0, seed=11)[0],
                      BackgroundParams(destripe=False))
    hi = reduce_frame(_frame(noise=16.0, stripe_amp=0.0, n_sources=0, seed=11)[0],
                      BackgroundParams(destripe=False))
    m_lo, m_hi = float(np.median(lo.rms)), float(np.median(hi.rms))
    assert m_lo == pytest.approx(4.0, abs=1.5)
    assert m_hi == pytest.approx(16.0, abs=3.0)
    assert m_hi > 2.5 * m_lo


# ── sources preserved ────────────────────────────────────────────────────────

def test_sources_preserved_through_reduction():
    shape = (200, 240)
    base, _, _ = _frame(shape=shape, stripe_amp=50.0, n_sources=0, noise=6.0, seed=6)
    cx, cy, amp = 120.0, 100.0, 500.0
    img = base + _gaussian(shape, cx, cy, amp)

    res = reduce_frame(img, BackgroundParams(destripe=True))
    peak = res.data_sub[int(cy), int(cx)]
    # peak survives with most of its amplitude (background+destripe shouldn't eat it)
    assert peak > 0.8 * amp, f"source peak eroded to {peak:.0f} / {amp:.0f}"


def test_returns_result_dataclass():
    img, _, _ = _frame(seed=7)
    res = reduce_frame(img)
    assert isinstance(res, BackgroundResult)
    assert res.data_sub.dtype == np.float32
    assert res.background.shape == img.shape == res.rms.shape


def test_accepts_imagedata(tmp_path):
    from astropy.io import fits

    from analysis.astrometry.io import load_image

    img, _, _ = _frame(stripe_amp=30.0, seed=8)
    before = column_pattern_amplitude(img)
    p = tmp_path / "f.fits"
    fits.PrimaryHDU(img.astype(np.float32)).writeto(p, overwrite=True)
    res = reduce_frame(load_image(str(p)))
    # confirm the reduction actually ran on the ImageData, not just returned shape
    assert res.data_sub.shape == img.shape
    assert res.destriped is True
    assert column_pattern_amplitude(res.data_sub) < 0.2 * before
