# ruff: noqa: E402, E501, E702, E741, B905
"""
Phase 0 tests — photometry.io universal image loading.

Covers: all four format families, colour->mono luminance, 16-bit preservation,
metadata extraction + absent-metadata warnings, and error paths (unsupported /
missing / corrupt).  Fixtures are synthesised in a tmp dir so the suite needs no
committed binaries.
"""

from __future__ import annotations

import numpy as np
import pytest

from analysis.astrometry.engine import image_stats
from analysis.astrometry.io import ImageData, ImageLoadError, load_image

# ── fixtures ────────────────────────────────────────────────────────────────

def _write_fits(path, data, headers=None):
    from astropy.io import fits
    hdu = fits.PrimaryHDU(data=data)
    for k, v in (headers or {}).items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)


def _write_raster(path, arr):
    from PIL import Image
    im = Image.fromarray(arr)  # dtype drives the mode (uint16 -> "I;16")
    im.save(path)


# ── format coverage ─────────────────────────────────────────────────────────

def test_load_fits_mono_with_metadata(tmp_path):
    data = np.arange(200 * 100, dtype=np.uint16).reshape(100, 200)  # H=100, W=200
    p = tmp_path / "frame.fits"
    _write_fits(p, data, {"EXPTIME": 1.5, "DATE-OBS": "2026-07-08T14:30:00"})

    img = load_image(str(p))
    assert isinstance(img, ImageData)
    assert (img.width, img.height) == (200, 100)
    assert img.data.dtype == np.float32
    assert img.data.ndim == 2
    assert img.source_format == "fits"
    assert img.exposure_s == pytest.approx(1.5)
    assert img.epoch == "2026-07-08T14:30:00"
    assert img.bit_depth == 16
    assert not img.is_color_original
    assert img.warnings == []


def test_load_png_color_to_mono_luminance(tmp_path):
    # Pure green frame → luminance should be ~0.7152 * 255.
    rgb = np.zeros((32, 48, 3), dtype=np.uint8)
    rgb[..., 1] = 255
    p = tmp_path / "green.png"
    _write_raster(p, rgb)

    img = load_image(str(p))
    assert (img.width, img.height) == (48, 32)
    assert img.is_color_original
    assert img.source_format == "png"
    assert float(np.median(img.data)) == pytest.approx(0.7152 * 255, rel=1e-3)


def test_load_tiff_16bit_preserves_values(tmp_path):
    arr = np.full((20, 30), 40000, dtype=np.uint16)
    arr[0, 0] = 65535
    p = tmp_path / "hi.tiff"
    _write_raster(p, arr)

    img = load_image(str(p))
    assert img.source_format == "tiff"
    assert img.bit_depth == 16
    assert float(img.data.max()) == pytest.approx(65535.0)
    assert float(np.median(img.data)) == pytest.approx(40000.0)


def test_load_jpeg_mono(tmp_path):
    rgb = np.random.default_rng(0).integers(0, 255, (24, 24, 3), dtype=np.uint8)
    p = tmp_path / "pic.jpg"
    _write_raster(p, rgb)

    img = load_image(str(p))
    assert img.source_format == "jpeg"
    assert img.data.ndim == 2
    assert img.is_color_original


# ── metadata absence → warnings, not failure ────────────────────────────────

def test_raster_metadata_absent_warns(tmp_path):
    p = tmp_path / "plain.png"
    _write_raster(p, np.zeros((8, 8), dtype=np.uint8))

    img = load_image(str(p))
    assert img.exposure_s is None
    assert img.epoch is None
    assert any("epoch" in w for w in img.warnings)


def test_fits_missing_exposure_warns(tmp_path):
    p = tmp_path / "noexp.fits"
    _write_fits(p, np.ones((10, 10), dtype=np.float32))

    img = load_image(str(p))
    assert img.exposure_s is None
    assert any("exposure" in w for w in img.warnings)


# ── error paths ─────────────────────────────────────────────────────────────

def test_unsupported_extension(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with pytest.raises(ImageLoadError, match="unsupported"):
        load_image(str(p))


def test_missing_file():
    with pytest.raises(ImageLoadError, match="not found"):
        load_image("does_not_exist.fits")


def test_corrupt_fits(tmp_path):
    p = tmp_path / "bad.fits"
    p.write_bytes(b"\x00\x01\x02not a fits\xff" * 4)
    with pytest.raises(ImageLoadError):
        load_image(str(p))


# ── CLI stats helper ────────────────────────────────────────────────────────

def test_image_stats_roundtrip(tmp_path):
    data = np.array([[0, 10], [20, 30]], dtype=np.uint16)
    p = tmp_path / "tiny.fits"
    _write_fits(p, data, {"EXPTIME": 2.0})

    stats = image_stats(load_image(str(p)))
    assert stats["dims"] == "2 x 2"
    assert stats["max"] == pytest.approx(30.0)
    assert stats["median"] == pytest.approx(15.0)
    assert stats["exposure_s"] == pytest.approx(2.0)


# ── extra coverage (reviewer-flagged gaps) ──────────────────────────────────

def test_load_fits_channel_first_color_to_mono(tmp_path):
    # FITS colour cube is channel-first (C,H,W); pure-green plane → 0.7152 luma.
    cube = np.zeros((3, 16, 24), dtype=np.uint8)
    cube[1] = 200
    p = tmp_path / "cube.fits"
    _write_fits(p, cube)

    img = load_image(str(p))
    assert img.is_color_original
    assert (img.width, img.height) == (24, 16)
    assert float(np.median(img.data)) == pytest.approx(0.7152 * 200, rel=1e-3)


def test_corrupt_raster(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n garbage not a real png")
    with pytest.raises(ImageLoadError):
        load_image(str(p))


# ── types contract ──────────────────────────────────────────────────────────

def test_types_contract_and_json():
    import json

    from analysis.astrometry.types import (
        MODE_OBJECT,
        ROLE_STAR,
        ROLE_TARGET,
        SHAPE_POINT,
        SHAPE_STREAK,
        Detection,
        DetectionResult,
    )

    star = Detection(id="S1", role=ROLE_STAR, shape=SHAPE_STREAK,
                     xs=0.0, ys=0.0, xe=4.0, ye=2.0, brt=2)
    tgt = Detection(id="T", role=ROLE_TARGET, shape=SHAPE_POINT, x=10.0, y=20.0, brt=5)
    assert star.midpoint() == (2.0, 1.0)
    assert tgt.midpoint() == (10.0, 20.0)

    res = DetectionResult(mode=MODE_OBJECT, image_dims=(24, 16),
                          n_stars=1, detections=[star, tgt])
    assert res.target is tgt
    assert res.stars == [star]
    # JSON round-trips and image_dims is a list, not a tuple
    payload = json.dumps(res.to_dict())
    assert json.loads(payload)["image_dims"] == [24, 16]
