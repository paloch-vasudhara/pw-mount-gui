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
"""

from __future__ import annotations

import logging
import os
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
SCALE_RATIO_LO, SCALE_RATIO_HI = 0.8, 1.25   # nova is given +-25 % on the hint
CENTROID_PX = 0.7         # ssa sigma_for: sub-pixel centroid estimate, x plate scale


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
                 resume_subid: Optional[int] = None) -> SolveResult:
    """Render the synthetic field at ``xy`` and solve it. Raises ``NovaError``."""
    fwhm = max(MIN_FWHM_PX, STAR_ARCSEC / scale_est_arcsec) if scale_est_arcsec else BLIND_FWHM_PX
    img = render_synthetic_points(xy, width, height, fwhm=fwhm)
    path = os.path.join(tempfile.gettempdir(), f"pwastro_syn_{uuid.uuid4().hex}.fits")
    _write_fits(img, path)
    try:
        extra = {} if resume_subid is None else {"resume_subid": resume_subid}
        sol = client.solve(path, width=width, height=height,
                           scale_est_arcsec=scale_est_arcsec, timeout=timeout,
                           progress=progress, check_cancel=check_cancel, **extra)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return SolveResult(sol.ra_deg, sol.dec_deg, sol.pixscale_arcsec, sol.wcs_header, len(xy))


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


def scale_ok(solved_arcsec: float, hint_arcsec: Optional[float]) -> bool:
    """False only when a hint exists and the solved scale is off by more than 25 %."""
    if not hint_arcsec:
        return True
    ratio = solved_arcsec / hint_arcsec
    return SCALE_RATIO_LO <= ratio <= SCALE_RATIO_HI


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
