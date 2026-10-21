"""Sky chunks: vectorised SGP4 -> TEME -> ECEF -> topocentric az/el, above-horizon spans only.

Binary response format (`application/octet-stream`, little-endian), for the T2.2b TS decoder:

    offset 0   uint32  H            header length in bytes (includes trailing space padding;
                                    4 + H is always a multiple of 4 so the arrays below are
                                    4-byte aligned and can be viewed with Int32Array/Float32Array)
    offset 4   H bytes UTF-8 JSON   {"layer": "leo", "t0_unix": <int, snapped to the step grid>,
                                    "step_s": <10|60|300>, "n_steps": <int>,
                                    "n_seg": <int>, "n_samples": <int>, "norad": [<int>, ...]}
    then       Int32   [n_seg * 3]  segments, row-major: (obj_idx, start_step, n)
    then       Float32 [n_samples * 2] samples, row-major: (az_deg, alt_deg)

Time grid: step k is at unix time `t0_unix + k * step_s`, k = 0 .. n_steps-1, where
`n_steps = ceil(duration_s / step_s) + 1` (so consecutive chunks share one boundary sample).
`obj_idx` indexes the header `norad` list (only objects with at least one segment are listed).
A segment covers steps start_step .. start_step+n-1 and consumes the next n entries of the
sample array, in order (segments are sorted by obj_idx, then start_step; the sample cursor
simply advances by n per segment). az is degrees clockwise from north in [0, 360); alt is
degrees above the horizon (geometric, no refraction).

Only above-horizon spans are kept: a sample is kept when alt > -2 deg, plus one sample of margin
on each side (when that sample is valid and inside the grid). Samples where SGP4 reports an
error (code != 0) are dropped and always break a segment.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass

import numpy as np

from .catalog import LayerData

STEP_S = {"leo": 10, "meo": 60, "geo": 300}
HORIZON_DEG = -2.0
MAX_DURATION_S = 3600
UNIX_JD0 = 2440587.5
WGS84_A_KM = 6378.137
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)


@dataclass(frozen=True)
class Site:
    lat: float
    lon: float
    alt_m: float


def snap_t0(t0_unix: float, step_s: int) -> int:
    return int(math.floor(t0_unix / step_s) * step_s)


def n_steps_for(duration_s: float, step_s: int) -> int:
    return int(math.ceil(duration_s / step_s)) + 1


def _gmst_rad(jd0: np.ndarray, fr: np.ndarray) -> np.ndarray:
    """IAU 1982 GMST (UT1 ~ UTC), the same model SGP4/TEME uses."""
    t = ((jd0 - 2451545.0) + fr) / 36525.0
    sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 0.093104 * t * t
        - 6.2e-6 * t**3
    )
    return np.mod(sec, 86400.0) / 86400.0 * (2.0 * math.pi)


def _site_ecef_and_basis(site: Site) -> tuple[np.ndarray, np.ndarray]:
    lat, lon = math.radians(site.lat), math.radians(site.lon)
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    n = WGS84_A_KM / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    h = site.alt_m / 1000.0
    ecef = np.array(
        [(n + h) * cl * co, (n + h) * cl * so, (n * (1.0 - WGS84_E2) + h) * sl], dtype=np.float64
    )
    basis = np.array(  # rows: east, north, up
        [[-so, co, 0.0], [-sl * co, -sl * so, cl], [cl * co, cl * so, sl]], dtype=np.float64
    )
    return ecef, basis


def time_grid(t0_unix: int, step_s: int, n_steps: int) -> tuple[np.ndarray, np.ndarray]:
    unix = t0_unix + step_s * np.arange(n_steps, dtype=np.float64)
    jd_full = UNIX_JD0 + unix / 86400.0
    jd0 = np.floor(jd_full - 0.5) + 0.5
    return jd0, jd_full - jd0


def compute_dense(
    layer: LayerData, site: Site, t0_unix: int, step_s: int, n_steps: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:  # norad, ok, az, alt
    """Per-object dense results over the whole grid: (ok, az, alt) of shape (n_obj, n_steps).

    az/alt are NaN where SGP4 failed. Used by tests and the trimming step (blockwise).
    """
    parts = list(_iter_blocks(layer, site, t0_unix, step_s, n_steps, trim=False))
    if not parts:
        z = np.zeros((0, n_steps))
        return layer.norad, z.astype(bool), z, z
    ok = np.concatenate([p[0] for p in parts])
    az = np.concatenate([p[1] for p in parts])
    alt = np.concatenate([p[2] for p in parts])
    return layer.norad, ok, az, alt


def _iter_blocks(layer, site, t0_unix, step_s, n_steps, trim):
    jd0, fr = time_grid(t0_unix, step_s, n_steps)
    theta = _gmst_rad(jd0, fr)
    c, s = np.cos(theta), np.sin(theta)
    site_ecef, basis = _site_ecef_and_basis(site)
    for block in layer.blocks:
        err, r, _v = block.sgp4(jd0, fr)
        ok = (err == 0) & np.isfinite(r).all(axis=2)
        # TEME -> ECEF (rotation about z by GMST), then relative to the site.
        x, y, z = r[..., 0], r[..., 1], r[..., 2]
        dx = c * x + s * y - site_ecef[0]
        dy = -s * x + c * y - site_ecef[1]
        dz = z - site_ecef[2]
        east = basis[0, 0] * dx + basis[0, 1] * dy
        north = basis[1, 0] * dx + basis[1, 1] * dy + basis[1, 2] * dz
        up = basis[2, 0] * dx + basis[2, 1] * dy + basis[2, 2] * dz
        with np.errstate(invalid="ignore"):
            alt = np.degrees(np.arctan2(up, np.hypot(east, north)))
        if not trim:
            with np.errstate(invalid="ignore"):
                az = np.mod(np.degrees(np.arctan2(east, north)), 360.0)
            alt = np.where(ok, alt, np.nan)
            az = np.where(ok, az, np.nan)
            yield ok, az, alt
            continue
        above = ok & (alt > HORIZON_DEG)
        keep = above.copy()
        keep[:, 1:] |= above[:, :-1]
        keep[:, :-1] |= above[:, 1:]
        keep &= ok
        yield keep, east, north, alt


def trim_mask(ok: np.ndarray, alt: np.ndarray) -> np.ndarray:
    """Reference trimming rule (used by tests): above horizon, +1 sample margin, valid only."""
    above = ok & (alt > HORIZON_DEG)
    keep = above.copy()
    keep[:, 1:] |= above[:, :-1]
    keep[:, :-1] |= above[:, 1:]
    return keep & ok


def compute_chunk(
    layer: LayerData, site: Site, t0_unix: float, duration_s: float, name: str | None = None
) -> bytes:
    name = name or layer.layer
    step = STEP_S[name]
    t0 = snap_t0(t0_unix, step)
    n = n_steps_for(duration_s, step)
    seg_obj: list[np.ndarray] = []
    seg_start: list[np.ndarray] = []
    seg_len: list[np.ndarray] = []
    samples: list[np.ndarray] = []
    base = 0
    for keep, east, north, alt in _iter_blocks(layer, site, t0, step, n, trim=True):
        rows = keep.shape[0]
        if keep.any():
            pad = np.zeros((rows, 1), dtype=np.int8)
            d = np.diff(np.concatenate([pad, keep.astype(np.int8), pad], axis=1), axis=1)
            so, ss = np.nonzero(d == 1)
            _eo, es = np.nonzero(d == -1)
            seg_obj.append(so + base)
            seg_start.append(ss)
            seg_len.append(es - ss)
            az_k = np.mod(np.degrees(np.arctan2(east[keep], north[keep])), 360.0)
            samples.append(np.stack([az_k, alt[keep]], axis=1).astype(np.float32))
        base += rows
    if seg_obj:
        obj = np.concatenate(seg_obj)
        start = np.concatenate(seg_start)
        length = np.concatenate(seg_len)
        uniq, inv = np.unique(obj, return_inverse=True)
        segs = np.stack([inv, start, length], axis=1).astype("<i4")
        smp = np.concatenate(samples).astype("<f4")
        norad = [int(x) for x in layer.norad[uniq]]
    else:
        segs = np.zeros((0, 3), dtype="<i4")
        smp = np.zeros((0, 2), dtype="<f4")
        norad = []
    header = {
        "layer": name,
        "t0_unix": t0,
        "step_s": step,
        "n_steps": n,
        "n_seg": int(len(segs)),
        "n_samples": int(len(smp)),
        "norad": norad,
    }
    hb = json.dumps(header, separators=(",", ":")).encode("utf-8")
    hb += b" " * ((-(4 + len(hb))) % 4)
    return struct.pack("<I", len(hb)) + hb + segs.tobytes() + smp.tobytes()


def decode_chunk(buf: bytes) -> dict:
    """Reference decoder (mirror of the T2.2b TS decoder): header, segments (n,3), samples (m,2)."""
    (hlen,) = struct.unpack_from("<I", buf, 0)
    header = json.loads(buf[4 : 4 + hlen].decode("utf-8"))
    off = 4 + hlen
    n_seg, n_smp = header["n_seg"], header["n_samples"]
    segs = np.frombuffer(buf, dtype="<i4", count=n_seg * 3, offset=off).reshape(n_seg, 3)
    off += n_seg * 12
    smp = np.frombuffer(buf, dtype="<f4", count=n_smp * 2, offset=off).reshape(n_smp, 2)
    if off + n_smp * 8 != len(buf):
        raise ValueError("trailing or missing bytes")
    return {"header": header, "segments": segs, "samples": smp}


def iter_tracks(decoded: dict):
    """Yield (norad, start_step, samples[n,2]) per segment."""
    cur = 0
    norad = decoded["header"]["norad"]
    for obj, start, n in decoded["segments"]:
        yield norad[obj], int(start), decoded["samples"][cur : cur + n]
        cur += int(n)
