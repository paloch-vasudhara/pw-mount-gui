"""
analysis/astrometry/io.py  (ported from ssa-toolkit photometry/io.py, object mode)
================
Universal image loading for the detection engine.

Supported inputs
----------------
  FITS  : .fit .fits .fts   (astropy; primary/first image HDU)
  Raster: .tif .tiff .jpg .jpeg .png   (Pillow)

Everything is reduced to a single-plane, float32, mono array in native counts
(NOT rescaled to 0..1 — SNR and background statistics need the real ADU scale).
Colour frames are converted to luminance (Rec.709).  Metadata (exposure epoch,
exposure time, bit depth) is pulled where the format carries it and defaulted
with a warning where it does not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

FITS_EXTS = {".fit", ".fits", ".fts"}
RASTER_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}
SUPPORTED_EXTS = FITS_EXTS | RASTER_EXTS

# Rec.709 luminance weights for colour -> mono.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)


class ImageLoadError(Exception):
    """Raised for unsupported, missing, or unreadable image files."""


@dataclass
class ImageData:
    """A loaded frame plus what we could learn about it."""

    data: np.ndarray                    # 2-D float32, mono, native counts
    width: int
    height: int
    source_format: str                  # "fits" | "tiff" | "jpeg" | "png"
    bit_depth: Optional[int] = None
    exposure_s: Optional[float] = None
    epoch: Optional[str] = None         # capture epoch string (DATE-OBS / EXIF)
    is_color_original: bool = False
    header: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def dims(self) -> tuple[int, int]:
        return (self.width, self.height)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def load_image(path: str) -> ImageData:
    """Load *path* into an :class:`ImageData`.

    Raises :class:`ImageLoadError` on a missing file, an unsupported extension,
    or unreadable/corrupt content.
    """
    if not os.path.isfile(path):
        raise ImageLoadError(f"file not found: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise ImageLoadError(
            f"unsupported format '{ext}'. Supported: "
            + ", ".join(sorted(SUPPORTED_EXTS))
        )

    if ext in FITS_EXTS:
        return _load_fits(path)
    return _load_raster(path, ext)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _to_mono(arr: np.ndarray) -> tuple[np.ndarray, bool]:
    """Reduce an array to a 2-D mono plane. Returns (mono, was_color)."""
    arr = np.asarray(arr)
    if arr.ndim == 2:
        return arr, False

    if arr.ndim == 3:
        # Locate the colour axis (size 3 = RGB, 4 = RGBA). FITS tends to be
        # channel-first (C,H,W); raster is channel-last (H,W,C).
        if arr.shape[-1] in (3, 4):          # channel-last
            rgb = arr[..., :3].astype(np.float64)
            mono = rgb @ _LUMA
            return mono, True
        if arr.shape[0] in (3, 4):           # channel-first
            rgb = arr[:3].astype(np.float64)
            mono = np.tensordot(_LUMA, rgb, axes=([0], [0]))
            return mono, True
        # A cube we don't recognise as colour — take the first plane.
        return arr[0], False

    raise ImageLoadError(f"unexpected image dimensionality: {arr.ndim}-D")


def _load_fits(path: str) -> ImageData:
    try:
        from astropy.io import fits
    except ImportError as exc:               # pragma: no cover
        raise ImageLoadError("astropy is required to read FITS files") from exc

    warnings: list[str] = []
    try:
        with fits.open(path, memmap=False) as hdul:
            hdu = _first_image_hdu(hdul)
            if hdu is None:
                raise ImageLoadError("FITS file contains no image data")
            raw = hdu.data
            hdr = hdu.header
    except ImageLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 — astropy raises many low-level types
        raise ImageLoadError(f"could not read FITS: {exc}") from exc

    if raw is None:
        raise ImageLoadError("FITS image HDU has no data")

    mono, was_color = _to_mono(raw)
    data = np.ascontiguousarray(mono, dtype=np.float32)

    bitpix = hdr.get("BITPIX")
    bit_depth = abs(int(bitpix)) if bitpix is not None else None

    exposure_s = _first_present(hdr, ("EXPTIME", "EXPOSURE", "ITIME"))
    if exposure_s is not None:
        try:
            exposure_s = float(exposure_s)
        except (TypeError, ValueError):
            exposure_s = None
    if exposure_s is None:
        warnings.append("exposure time not found in FITS header")

    epoch = _first_present(hdr, ("DATE-OBS", "DATE_OBS", "DATEOBS"))
    if epoch is not None:
        epoch = str(epoch)
    else:
        warnings.append("capture epoch (DATE-OBS) not found in FITS header")

    h, w = data.shape
    return ImageData(
        data=data, width=w, height=h, source_format="fits",
        bit_depth=bit_depth, exposure_s=exposure_s, epoch=epoch,
        is_color_original=was_color,
        header={k: hdr.get(k) for k in ("BITPIX", "EXPTIME", "DATE-OBS", "FOCALLEN", "XPIXSZ")
                if k in hdr},
        warnings=warnings,
    )


def _first_image_hdu(hdul):
    """Return the first HDU that actually holds image pixels."""
    for hdu in hdul:
        if getattr(hdu, "data", None) is not None and getattr(hdu.data, "ndim", 0) >= 2:
            return hdu
    return None


def _first_present(header, keys):
    for k in keys:
        if k in header and header[k] not in (None, ""):
            return header[k]
    return None


def _load_raster(path: str, ext: str) -> ImageData:
    try:
        from PIL import Image
    except ImportError as exc:               # pragma: no cover
        raise ImageLoadError("Pillow is required to read raster images") from exc

    warnings: list[str] = []
    try:
        with Image.open(path) as im:
            im.load()
            pil_mode = im.mode
            arr = np.asarray(im)
            exif_exposure = _exif_exposure(im)
            exif_epoch = _exif_epoch(im)
    except Exception as exc:  # noqa: BLE001
        raise ImageLoadError(f"could not read image: {exc}") from exc

    if arr.size == 0:
        raise ImageLoadError("image contains no pixels")

    mono, was_color = _to_mono(arr)
    data = np.ascontiguousarray(mono, dtype=np.float32)

    # Bit depth from the source dtype / PIL mode.
    bit_depth = _bit_depth_from(arr.dtype, pil_mode)

    fmt = {
        ".tif": "tiff", ".tiff": "tiff",
        ".jpg": "jpeg", ".jpeg": "jpeg",
        ".png": "png",
    }[ext]

    if exif_exposure is None:
        warnings.append("exposure time not available (no EXIF ExposureTime)")
    if exif_epoch is None:
        warnings.append("no capture epoch in raster metadata — supply it manually")

    h, w = data.shape
    return ImageData(
        data=data, width=w, height=h, source_format=fmt,
        bit_depth=bit_depth, exposure_s=exif_exposure, epoch=exif_epoch,
        is_color_original=was_color,
        header={"pil_mode": pil_mode},
        warnings=warnings,
    )


def _bit_depth_from(dtype, pil_mode) -> Optional[int]:
    if pil_mode in ("I", "I;16", "I;16B", "I;16L"):
        return 16
    if np.issubdtype(dtype, np.integer):
        return int(np.iinfo(dtype).bits)
    if np.issubdtype(dtype, np.floating):
        return int(np.finfo(dtype).bits)
    return None


def _exif_dict(im) -> dict:
    """All EXIF tags as a flat dict, merging the main IFD with the Exif sub-IFD.

    Real camera files keep ExposureTime, FNumber, ISO, DateTimeOriginal, etc. in
    the Exif sub-IFD (pointer 0x8769), NOT the main IFD that ``getexif()`` returns
    directly — so a bare ``exif.get(33434)`` misses them. We merge both so callers
    find the tag wherever it lives (main IFD wins on the rare duplicate)."""
    getexif = getattr(im, "getexif", None) or getattr(im, "_getexif", None)
    if getexif is None:
        return {}
    try:
        exif = getexif()
    except Exception:  # noqa: BLE001
        return {}
    if not exif:
        return {}
    merged = {}
    try:
        sub = exif.get_ifd(0x8769)  # ExifIFD — where ExposureTime / DateTimeOriginal live
        if sub:
            merged.update(sub)
    except Exception:  # noqa: BLE001 — older Pillow / no sub-IFD
        pass
    merged.update(dict(exif))       # main IFD takes precedence on conflict
    return merged


def _exif_exposure(im) -> Optional[float]:
    """Pull ExposureTime (EXIF tag 33434) as seconds, if present."""
    val = _exif_dict(im).get(33434)  # ExposureTime
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _exif_epoch(im) -> Optional[str]:
    """Capture epoch from EXIF, normalised to ``YYYY-MM-DD HH:MM:SS[.fff]``.

    Prefers DateTimeOriginal (36867 — the instant the shutter opened), then
    DateTimeDigitized (36868), then DateTime (306 — file-modified). EXIF stores
    these as ``YYYY:MM:DD HH:MM:SS``; we convert the date separators to dashes.
    Sub-second precision (SubsecTimeOriginal, 37521) is appended when present.
    Returns None when no usable tag exists (caller falls back to file mtime)."""
    d = _exif_dict(im)
    raw = None
    for tag in (36867, 36868, 306):  # DateTimeOriginal, DateTimeDigitized, DateTime
        v = d.get(tag)
        if v:
            raw = str(v).strip()
            break
    if not raw:
        return None
    # "YYYY:MM:DD HH:MM:SS" -> "YYYY-MM-DD HH:MM:SS" (only the date half uses ':').
    parts = raw.split(" ", 1)
    date = parts[0].replace(":", "-")
    epoch = date if len(parts) == 1 else f"{date} {parts[1]}"
    subsec = d.get(37521)  # SubsecTimeOriginal
    if subsec:
        s = str(subsec).strip()
        if s.isdigit():
            epoch = f"{epoch}.{s}"
    return epoch
