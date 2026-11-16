"""
analysis/astrometry/solve.py  (T5.2b)
=====================================
Object-mode solve + target mapping. Ported from ssa-toolkit ``photometry/solve.py``
(``render_synthetic_points``, ``_write_fits``, ``solve_points``, ``_solution_wcs``,
``map_point``, ``sigma_for``) and ``data_api/routes/astrometry.py`` (``_scale_check``,
``_shift_epoch``), trimmed to this flow.

The mount tracked the object, so stars are streaks: each star-streak MIDPOINT (the star
position at mid-exposure) becomes a Gaussian in a synthetic point field, nova solves that
field, and the TARGET pixel is mapped through the returned WCS. Pixel convention: 0-indexed,
a point at (x, y) sits at ``data[y, x]`` (astropy ``origin=0``).

T5.2g: what nova is sent. Default (``PW_ASTROMETRY_UPLOAD=xylist``): a FITS binary table of
the star midpoints (X, Y 1-based FITS pixels = our 0-based + 1, FLUX; IMAGEW / IMAGEH), a few
KB. Fallback (``image``): the synthetic field downscaled to <= 2048 px on the long side as a
uint8 FITS (~3 MB for a 9576 x 6388 sensor, not 245 MB); the returned WCS is rescaled back to
full-resolution pixels (SIP terms are dropped). Either way the WCS handed to ``build_wcs`` /
``map_point`` is in the original 0-based pixel frame, exactly as before.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np

from .nova import NovaClient

log = logging.getLogger("analysis.astrometry.solve")

ARCSEC_PER_RAD_MM = 206265.0
STAR_ARCSEC = 3.0         # synthetic star FWHM when the scale is known (ssa _STAR_ARCSEC)
MIN_FWHM_PX = 2.0
BLIND_FWHM_PX = 3.0
SCALE_RATIO_LO, SCALE_RATIO_HI = 0.8, 1.25   # nova's +-25 % on the hint; the station match
CENTROID_PX = 0.7         # ssa sigma_for: sub-pixel centroid estimate, x plate scale

UPLOAD_ENV = "PW_ASTROMETRY_UPLOAD"
MODE_XYLIST, MODE_IMAGE = "xylist", "image"
IMAGE_MAX_PX = 2048       # image mode: the long side of the uploaded field
_SIP_KEY = re.compile(r"^(A|B|AP|BP)_(\d+_\d+|ORDER|DMAX)$")


def upload_mode() -> str:
    """``xylist`` (default) | ``image`` from the env; anything else -> xylist."""
    v = os.environ.get(UPLOAD_ENV, "").strip().lower()
    return MODE_IMAGE if v == MODE_IMAGE else MODE_XYLIST


def downscale_factor(width: int, height: int) -> float:
    """Image mode: f = max(1, long side / 2048)."""
    return max(1.0, max(width, height) / float(IMAGE_MAX_PX))


def downscaled_size(width: int, height: int) -> tuple[int, int]:
    f = downscale_factor(width, height)
    return int(math.ceil(width / f)), int(math.ceil(height / f))


def hint_radius_deg(scale_arcsec: Optional[float], width: int, height: int) -> float:
    """Search radius around a header pointing: 1.5 x half the field diagonal, 1..10 deg;
    10 deg when the scale is unknown."""
    if not scale_arcsec:
        return 10.0
    half_diag = math.hypot(width, height) * scale_arcsec / 3600.0 / 2.0
    return float(min(10.0, max(1.0, 1.5 * half_diag)))


def hint_scale_arcsec(focal_mm: Optional[float], pixel_um: Optional[float]) -> Optional[float]:
    """Plate scale from the optics, or None (blind)."""
    if focal_mm and pixel_um and focal_mm > 0 and pixel_um > 0:
        return ARCSEC_PER_RAD_MM * (pixel_um / 1000.0) / focal_mm
    return None


def render_synthetic_points(xy, width: int, height: int, fwhm: float = 3.0,
                            amp: float = 20000.0, sky: float = 100.0, noise: float = 5.0,
                            seed: int = 0) -> np.ndarray:
    img = np.full((height, width), sky, dtype=np.float32)
    if noise > 0:
        img += np.random.default_rng(seed).normal(0, noise, (height, width)).astype(np.float32)
    sigma = max(fwhm / 2.3548, 0.6)
    r = int(np.ceil(4 * sigma))
    for x, y in xy:
        xi, yi = int(round(x)), int(round(y))
        x0, x1 = max(0, xi - r), min(width, xi + r + 1)
        y0, y1 = max(0, yi - r), min(height, yi + r + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        img[y0:y1, x0:x1] += amp * np.exp(
            -(((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2))).astype(np.float32)
    return img


def _write_fits(data: np.ndarray, path: str) -> None:
    from astropy.io import fits
    fits.PrimaryHDU(data=data.astype(np.float32)).writeto(path, overwrite=True)


def write_xylist(xy, width: int, height: int, path: str, flux=None) -> int:
    """Binary table ``X``, ``Y`` (float32, 1-based FITS pixels = our 0-based + 1, the
    astrometry.net xylist convention) and ``FLUX`` (when given), brightest first (stable for
    ties / no flux); header ``IMAGEW`` / ``IMAGEH``. Returns the number of rows."""
    from astropy.io import fits
    pts = [(float(x), float(y)) for x, y in xy]
    if flux is not None:
        order = sorted(range(len(pts)), key=lambda i: -float(flux[i]))
        pts = [pts[i] for i in order]
        fl = np.array([float(flux[i]) for i in order], dtype=np.float32)
    arr = np.array(pts, dtype=np.float64).reshape(-1, 2)
    cols = [fits.Column(name="X", format="E", array=(arr[:, 0] + 1.0).astype(np.float32)),
            fits.Column(name="Y", format="E", array=(arr[:, 1] + 1.0).astype(np.float32))]
    if flux is not None:
        cols.append(fits.Column(name="FLUX", format="E", array=fl))
    hdu = fits.BinTableHDU.from_columns(cols)
    hdu.header["IMAGEW"] = int(width)
    hdu.header["IMAGEH"] = int(height)
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)
    return len(pts)


def render_downscaled(xy, width: int, height: int, fwhm_px: float) -> tuple[np.ndarray, float]:
    """Image mode: the synthetic field at 1/f scale as uint8 (sky 20, sigma 2). Star
    (x0, y0) lands at ((x0 + 0.5) / f - 0.5, same for y). -> (image, f)."""
    f = downscale_factor(width, height)
    w, h = downscaled_size(width, height)
    pts = [((x + 0.5) / f - 0.5, (y + 0.5) / f - 0.5) for x, y in xy]
    img = render_synthetic_points(pts, w, h, fwhm=max(MIN_FWHM_PX, fwhm_px / f),
                                  amp=200.0, sky=20.0, noise=2.0)
    return np.clip(np.rint(img), 0, 255).astype(np.uint8), f


def _write_u8_fits(data: np.ndarray, path: str) -> None:
    from astropy.io import fits
    fits.PrimaryHDU(data=data).writeto(path, overwrite=True)


def rescale_wcs_header(hdr: dict, f: float) -> dict:
    """A WCS solved on the 1/f image -> the full-resolution frame: the CD matrix (or CDELT)
    / f, CRPIX_full = (CRPIX_ds - 0.5) * f + 0.5 (FITS 1-based pixel centres). SIP terms are
    dropped (and ``-SIP`` removed from CTYPE): the downscaled solution's distortion is not
    carried over."""
    if f == 1.0:
        return dict(hdr)
    out = {k: v for k, v in hdr.items() if not _SIP_KEY.match(k)}
    for k in ("CTYPE1", "CTYPE2"):
        if isinstance(out.get(k), str):
            out[k] = out[k].replace("-SIP", "")
    for k in ("CRPIX1", "CRPIX2"):
        if isinstance(out.get(k), (int, float)):
            out[k] = (float(out[k]) - 0.5) * f + 0.5
    cd = [k for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2") if k in out]
    if cd:
        for k in cd:
            out[k] = float(out[k]) / f
    else:
        for k in ("CDELT1", "CDELT2"):
            if isinstance(out.get(k), (int, float)):
                out[k] = float(out[k]) / f
    return out


@dataclass
class SolveResult:
    ra0_deg: float
    dec0_deg: float
    scale_arcsec_px: float
    wcs_header: dict
    n_stars_used: int


def solve_points(client: NovaClient, xy, width: int, height: int,
                 scale_est_arcsec: Optional[float], timeout: float,
                 progress: Optional[Callable] = None,
                 check_cancel: Optional[Callable] = None,
                 resume_subid: Optional[int] = None, *, flux=None, hint=None,
                 mode: Optional[str] = None) -> SolveResult:
    """Send the star list (or the downscaled field) and solve it. ``hint`` = (ra, dec,
    radius) deg from the image header; ``flux`` = relative brightness per star (the xylist
    order). Raises ``NovaError``. The result's WCS is in full-resolution 0-based pixels."""
    mode = mode or upload_mode()
    extra: dict = {}
    if resume_subid is not None:
        extra["resume_subid"] = resume_subid
    if hint is not None:
        extra["hint"] = tuple(hint)
    path = None
    f = 1.0
    cw, ch, cscale = width, height, scale_est_arcsec
    ext = "xyls" if mode == MODE_XYLIST else "fits"
    if resume_subid is None:
        path = os.path.join(tempfile.gettempdir(), f"pwastro_{uuid.uuid4().hex}.{ext}")
        if mode == MODE_XYLIST:
            write_xylist(xy, width, height, path, flux)
        else:
            fwhm = (max(MIN_FWHM_PX, STAR_ARCSEC / scale_est_arcsec) if scale_est_arcsec
                    else BLIND_FWHM_PX)
            img, f = render_downscaled(xy, width, height, fwhm)
            _write_u8_fits(img, path)
        log.info("astrometry upload mode=%s payload_bytes=%d stars=%d hint=%s",
                 mode, os.path.getsize(path), len(xy), "yes" if hint else "no")
    else:
        path = f"resume.{ext}"          # never opened: a resume only re-polls
        log.info("astrometry resume mode=%s stars=%d", mode, len(xy))
    if mode == MODE_IMAGE:
        f = downscale_factor(width, height)
        cw, ch = downscaled_size(width, height)
        cscale = scale_est_arcsec * f if scale_est_arcsec else None
    try:
        sol = client.solve(path, width=cw, height=ch, scale_est_arcsec=cscale,
                           timeout=timeout, progress=progress, check_cancel=check_cancel,
                           **extra)
    finally:
        if resume_subid is None:
            try:
                os.unlink(path)
            except OSError:
                pass
    hdr = rescale_wcs_header(sol.wcs_header, f) if mode == MODE_IMAGE else sol.wcs_header
    return SolveResult(sol.ra_deg, sol.dec_deg, sol.pixscale_arcsec / f, hdr, len(xy))


def build_wcs(header: dict):
    """astropy WCS from the solver's header dict, or None."""
    if not header:
        return None
    try:
        from astropy.io import fits
        from astropy.wcs import WCS
        h = fits.Header()
        for k, v in header.items():
            try:
                h[k] = v
            except Exception:  # noqa: BLE001
                pass
        return WCS(h)
    except Exception:  # noqa: BLE001
        log.warning("could not build a WCS from the solver header")
        return None


def map_point(wcs, x: float, y: float) -> tuple[float, float]:
    """Pixel (0-indexed) -> (RA deg in [0, 360), Dec deg) through the full WCS."""
    ra, dec = wcs.all_pix2world([[float(x), float(y)]], 0)[0]
    return float(ra) % 360.0, float(dec)


def scale_check(source: str, solved_arcsec: float, width: int, height: int,
                station_arcsec: Optional[float]) -> dict:
    """The solved scale vs the station's, for the user's Use / Discard review. ``match`` is
    None when the station scale is unknown. A solve is never failed for its scale."""
    def field_deg(scale: float) -> list[float]:
        return [round(width * scale / 3600.0, 4), round(height * scale / 3600.0, 4)]

    match = None
    if station_arcsec:
        match = SCALE_RATIO_LO <= solved_arcsec / station_arcsec <= SCALE_RATIO_HI
    return {
        "source": source,
        "solved_arcsec_px": round(solved_arcsec, 4),
        "field_deg": field_deg(solved_arcsec),
        "station_arcsec_px": round(station_arcsec, 4) if station_arcsec else None,
        "station_field_deg": field_deg(station_arcsec) if station_arcsec else None,
        "match": match,
    }


def sigma_arcsec(scale_arcsec_px: float) -> float:
    return CENTROID_PX * scale_arcsec_px


def parse_epoch(iso: str) -> datetime:
    """``YYYY-MM-DDTHH:MM:SS[.fff]Z`` (service.normalize_epoch output) -> aware UTC datetime."""
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in iso else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(iso.rstrip("Z"), fmt).replace(tzinfo=timezone.utc)


def mid_exposure(epoch_iso: str, exposure_s: Optional[float]) -> tuple[str, float, list[str]]:
    """(iso, unix, warnings): epoch + exposure / 2; no exposure -> the epoch + a warning."""
    t0 = parse_epoch(epoch_iso)
    warnings: list[str] = []
    if exposure_s and exposure_s > 0:
        t = t0 + timedelta(seconds=exposure_s / 2.0)
    else:
        t = t0
        warnings.append("No exposure time - the epoch is the exposure start, not mid-exposure")
    iso = t.strftime("%Y-%m-%dT%H:%M:%S") + f".{t.microsecond // 1000:03d}Z"
    return iso, t.timestamp(), warnings
