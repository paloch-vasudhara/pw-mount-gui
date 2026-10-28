"""Satellite pass maths and the TRACK OBJECT rule table (T4.1). Pure: no I/O, no clock; the
timescale and the Sun come in as arguments.

`compute_pass` finds the CURRENT pass of an Earth satellite over a site with geometric alt/az
(the same as the Sky Viewer card and chunks; PWI4's refracted altitude is slightly higher, so
this is the safe side, P18): rise / set (alt = 0), the first Lim EL crossings and the peak, all
refined to ~0.05 s. `track_verdict` is the rule table beneath the live TRACK OBJECT button.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .ephem import angular_sep_deg

SEARCH_S = 12 * 3600.0  # how far back / forward the rise and set are searched
COARSE_STEP_S = 10.0
# Chunk edges of the outward search: a LEO pass is found in the first chunk (cheap), a GEO / HEO
# object (no set) walks all the way to SEARCH_S.
CHUNK_EDGES_S = (0.0, 1800.0, 5400.0, 12600.0, 27000.0, SEARCH_S)
# The coarse scan uses a fast TEME -> ITRS pipeline (sgp4 + a GMST rotation; within ~0.005 deg
# of skyfield's, which also applies precession / nutation to the TEME vector); every crossing /
# the peak is then re-evaluated with skyfield itself on a 0.5 s grid (skyfield's nutation costs
# ~35 us per sample, too slow for the 12 h scan).
FINE_STEP_S = 0.5
FINE_PAD_S = 5.0  # the fine bracket reaches this far beyond the coarse samples (pipeline error)
SUN_KEEPOUT_DEG = 15.0  # Q14 (same disc as the gotos)
MIN_TRACK_S = 30.0  # Q15: PWI4 needs 10-16 s to acquire (P6)

R_BELOW_HORIZON = "Below the horizon"
R_SUN = "Within 15° of the Sun"
R_PEAK_BELOW = "Pass peaks below the station limit elevation"
R_SETTING = "Setting below the station limit elevation"
R_TOO_SHORT = "Pass ends in under 30 s"


@dataclass(frozen=True)
class PassInfo:
    """The current pass (Unix seconds; None = not applicable). `alt` / `az` / `rising` /
    `sun_sep` describe the object NOW and are refreshed by `refresh_now` every check."""

    alt: float
    az: float
    rising: bool
    sun_sep: float | None
    t_rise: float | None = None
    t_in: float | None = None
    t_peak: float | None = None
    peak_alt: float | None = None
    t_out: float | None = None
    t_set: float | None = None


def _times(ts: Any, t_unix: np.ndarray):
    # Unix seconds are calendar seconds (no leap seconds). Skyfield takes the UTC -> TT offset
    # of the calendar DATE given and normalises seconds overflowing it, so the date must be the
    # array's own day (the base 1970 would miss the 27 leap seconds since).
    day0 = math.floor(float(np.min(t_unix)) / 86400.0) * 86400
    d = datetime.fromtimestamp(day0, UTC)
    return ts.utc(d.year, d.month, d.day, 0, 0, t_unix - day0)


def _altaz(sat: Any, topos: Any, ts: Any, t_unix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    alt, az, _ = (sat - topos).at(_times(ts, t_unix)).altaz()
    return alt.degrees, az.degrees


def _coarse_alt(sat: Any, topos: Any, ts: Any, t_unix: np.ndarray) -> np.ndarray:
    """Fast geometric altitude (deg), within ~0.005 deg of `_altaz`: the sgp4 TEME vector
    rotated by GMST only. NaN where sgp4 fails."""
    t = _times(ts, t_unix)
    jd, fr = t.whole, t.tt_fraction - t.delta_t / 86400.0
    _, r, _ = sat.model.sgp4_array(jd, fr)  # TEME km, shape (N, 3)
    theta = np.radians(t.gmst * 15.0)
    c, s = np.cos(theta), np.sin(theta)
    x = r[:, 0] * c + r[:, 1] * s
    y = -r[:, 0] * s + r[:, 1] * c
    lat, lon = topos.latitude.radians, topos.longitude.radians
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    d = np.stack([x, y, r[:, 2]], axis=1) - topos.itrs_xyz.km
    return np.degrees(np.arcsin(np.clip(d @ up / np.linalg.norm(d, axis=1), -1.0, 1.0)))


def _make(line1: str, line2: str, site: tuple[float, float, float], ts: Any) -> tuple[Any, Any]:
    from skyfield.api import EarthSatellite, wgs84

    lat, lon, h = site
    return EarthSatellite(line1, line2, ts=ts), wgs84.latlon(lat, lon, elevation_m=h)


def _now(sat: Any, topos: Any, ts: Any, t_unix: float) -> tuple[float, float, bool]:
    alt, az = _altaz(sat, topos, ts, np.array([t_unix, t_unix + 1.0]))
    return float(alt[0]), float(az[0]), bool(alt[1] > alt[0])


def _sep(az: float, alt: float, sun_fn: Callable[[], tuple[float, float] | None]):
    sun = sun_fn()
    return None if sun is None else angular_sep_deg(az, alt, sun[1], sun[0])


def refresh_now(
    info: PassInfo,
    line1: str,
    line2: str,
    site: tuple[float, float, float],
    t_unix: float,
    sun_fn: Callable[[], tuple[float, float] | None],
    ts: Any,
) -> PassInfo:
    """The cached pass geometry with the live fields (alt / az / rising / sun_sep) re-evaluated."""
    sat, topos = _make(line1, line2, site, ts)
    alt, az, rising = _now(sat, topos, ts, t_unix)
    return replace(info, alt=alt, az=az, rising=rising, sun_sep=_sep(az, alt, sun_fn))


def _scan(sat: Any, topos: Any, ts: Any, t0: float, sign: float):
    """Alt samples outward from t0 (sign +1 forward / -1 back) in COARSE_STEP_S steps, stopping
    at the first sample below the horizon (kept as the last one). Returns (offsets_s, alts)."""
    offs: list[np.ndarray] = []
    alts: list[np.ndarray] = []
    for lo, hi in zip(CHUNK_EDGES_S[:-1], CHUNK_EDGES_S[1:], strict=True):
        off = np.arange(lo + COARSE_STEP_S, hi + COARSE_STEP_S / 2, COARSE_STEP_S) * sign
        alt = _coarse_alt(sat, topos, ts, t0 + off)
        offs.append(off)
        alts.append(alt)
        neg = np.nonzero(alt < 0.0)[0]
        if neg.size:
            offs[-1], alts[-1] = off[: neg[0] + 1], alt[: neg[0] + 1]
            break
    return np.concatenate(offs), np.concatenate(alts)


def _grid(a: float, b: float) -> np.ndarray:
    return np.linspace(a, b, int(round((b - a) / FINE_STEP_S)) + 1)


def _cross(t: np.ndarray, f: np.ndarray, level: float, upward: bool) -> float | None:
    """Linear-interpolated time of the first `level` crossing in a fine grid (None if none)."""
    d = f - level
    hit = (d[:-1] < 0) & (d[1:] >= 0) if upward else (d[:-1] >= 0) & (d[1:] < 0)
    idx = np.nonzero(hit)[0]
    if not idx.size:
        return None
    j = idx[0]
    return float(t[j] + (t[j + 1] - t[j]) * (-d[j] / (d[j + 1] - d[j])))


# Wider fine grids (pad_s, step_s) for a crossing the coarse pipeline put too far off: only a
# nearly flat altitude curve (a GEO wobbling around Lim EL) needs them.
WIDE_GRIDS = ((90.0, 1.0), (900.0, 5.0))


def _refine_wide(
    sat: Any, topos: Any, ts: Any, a: float, b: float, level: float, upward: bool
) -> float:
    for pad, step in WIDE_GRIDS:
        g = np.arange(a - pad, b + pad + step / 2, step)
        x = _cross(g, _altaz(sat, topos, ts, g)[0], level, upward)
        if x is not None:
            return x
    return (a + b) / 2.0  # numerically none: the coarse bracket's middle


def _vertex(t: np.ndarray, f: np.ndarray) -> tuple[float, float]:
    """(time, value) of the maximum of a fine grid, parabola through the top 3 samples."""
    j = int(np.argmax(f))
    if j == 0 or j == len(f) - 1:
        return float(t[j]), float(f[j])
    y0, y1, y2 = f[j - 1], f[j], f[j + 1]
    den = y0 - 2.0 * y1 + y2
    if den >= 0.0:
        return float(t[j]), float(y1)
    s = 0.5 * (y0 - y2) / den  # in grid steps, |s| <= 0.5
    return float(t[j] + s * (t[1] - t[0])), float(y1 - 0.25 * (y0 - y2) * s)


def compute_pass(
    line1: str,
    line2: str,
    site: tuple[float, float, float],
    lim_el: float,
    t_unix: float,
    sun_fn: Callable[[], tuple[float, float] | None],
    ts: Any,
) -> PassInfo:
    """The pass the object is in at `t_unix` (site = (lat, lon_east, height_m)). Below the
    horizon now: only the live fields (no pass is searched; the verdict is "Below the horizon").
    An object that does not rise / set within 12 h gets `t_rise` / `t_set` None; then the peak is
    the highest altitude of the forward 12 h and the Lim EL crossings are those inside it."""
    sat, topos = _make(line1, line2, site, ts)
    alt0, az0, rising = _now(sat, topos, ts, t_unix)
    base = PassInfo(alt0, az0, rising, _sep(az0, alt0, sun_fn))
    if alt0 < 0.0:
        return base

    if not math.isfinite(alt0):
        raise ValueError("propagation failed")
    bo, ba = _scan(sat, topos, ts, t_unix, -1.0)
    fo, fa = _scan(sat, topos, ts, t_unix, +1.0)
    T = np.concatenate([t_unix + bo[::-1], [t_unix], t_unix + fo])
    A = np.concatenate([ba[::-1], [alt0], fa])
    n0 = len(bo)
    rise_found, set_found = bool(A[0] < 0.0), bool(A[-1] < 0.0)
    lo = 1 if rise_found else n0  # first sample of the pass (or of the forward window)
    hi = len(T) - 2 if set_found else len(T) - 1
    pk_lo = lo if set_found else n0  # peak: the whole pass, or the forward window if no set

    # coarse brackets: (key, t_a, t_b, level, upward)
    br: list[tuple[str, float, float, float, bool]] = []
    if rise_found:
        br.append(("rise", T[0], T[1], 0.0, True))
    if set_found:
        br.append(("set", T[-2], T[-1], 0.0, False))
    d = A[lo : hi + 1] - lim_el
    up = np.nonzero((d[:-1] < 0) & (d[1:] >= 0))[0]
    down = np.nonzero((d[:-1] >= 0) & (d[1:] < 0))[0]
    if up.size:
        k = lo + int(up[0])
        br.append(("in", T[k], T[k + 1], lim_el, True))
        down = down[down > up[0]]
    if down.size:
        k = lo + int(down[0])
        br.append(("out", T[k], T[k + 1], lim_el, False))
    ip = pk_lo + int(np.argmax(A[pk_lo : hi + 1]))
    pa, pb = T[max(ip - 1, 0)], T[min(ip + 1, len(T) - 1)]

    grids = [_grid(a - FINE_PAD_S, b + FINE_PAD_S) for _, a, b, _, _ in br]
    grids.append(_grid(pa, pb))
    fine_alt, _ = _altaz(sat, topos, ts, np.concatenate(grids))
    out: dict[str, float] = {}
    pos = 0
    for (key, a, b, level, upward), g in zip(br, grids[:-1], strict=True):
        x = _cross(g, fine_alt[pos : pos + len(g)], level, upward)
        out[key] = x if x is not None else _refine_wide(sat, topos, ts, a, b, level, upward)
        pos += len(g)
    t_peak, peak_alt = _vertex(grids[-1], fine_alt[pos:])
    t_in = out.get("in")
    if t_in is None and rise_found and d[0] >= 0.0:
        t_in = out["rise"]  # Lim EL at / below the horizon: the pass starts above it
    return replace(
        base,
        t_rise=out.get("rise"),
        t_in=t_in,
        t_peak=t_peak,
        peak_alt=peak_alt,
        t_out=out.get("out"),
        t_set=out.get("set"),
    )


def track_verdict(
    info: PassInfo, now: float, lim_el: float
) -> tuple[str | None, str | None, str | None]:
    """The rule table (rows 2-6 of the T4.1 spec; row 0 = the mount guards and row 1 = the TLE
    fetch are the caller's). Returns (reason, code, phase): the first failing row wins, `phase`
    is "track" / "pre_position" when allowed, else None."""
    if info.alt < 0.0:
        return R_BELOW_HORIZON, "below_horizon", None
    if info.sun_sep is not None and info.sun_sep < SUN_KEEPOUT_DEG:
        return R_SUN, "sun", None
    if info.peak_alt is not None and info.peak_alt < lim_el:
        return R_PEAK_BELOW, "peak_below_limit", None
    # "past the peak": its time has gone, or (a never-setting object whose forward peak is
    # ahead) it is falling now
    past_peak = (info.t_peak is not None and info.t_peak <= now) or not info.rising
    if info.alt < lim_el and past_peak:
        return R_SETTING, "setting", None
    if info.t_out is None:
        left = math.inf
    else:
        left = info.t_out - max(now, info.t_in if info.t_in is not None else now)
    if left < MIN_TRACK_S:
        return R_TOO_SHORT, "pass_too_short", None
    return None, None, ("track" if info.alt >= lim_el else "pre_position")
