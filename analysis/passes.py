"""Satellite pass list for the Pass Analysis modal (T5.1a). Pure: no I/O, no clock; the timescale
and the ephemeris come in as arguments.

Geometric alt/az (no refraction, the chunks / state convention) from skyfield + sgp4. The window is
padded by PAD_S on each side and scanned every COARSE_S; a pass is a segment with alt >= Lim EL
that overlaps [start, end]. Crossings are refined on a REFINE_S grid with linear interpolation
(<= 0.5 s), the peak with a parabola vertex. Ideas copied from `gateway/passes.py` (the analysis
service must not import `gateway`).

OPTICAL also gives `visible` = the sub-intervals (inside the pass, inside the window) where the
object is sunlit and the Sun's geometric alt < `sun_alt_max`, sampled every COARSE_S and refined
to <= 1 s; passes with no visible time are dropped.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np

PAD_S = 7200.0
COARSE_S = 10.0
REFINE_S = 0.5
VIS_REFINE_S = 1.0
MAX_WINDOW_S = 7 * 86400.0


def _times(ts: Any, t_unix: np.ndarray):
    # Unix seconds are calendar seconds (no leap seconds): give skyfield the array's own first
    # calendar day and let it normalise the overflowing seconds.
    day0 = math.floor(float(np.min(t_unix)) / 86400.0) * 86400
    d = datetime.fromtimestamp(day0, UTC)
    return ts.utc(d.year, d.month, d.day, 0, 0, t_unix - day0)


def _altaz(sat: Any, topos: Any, ts: Any, t_unix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if t_unix.size == 0:
        return np.empty(0), np.empty(0)
    alt, az, _ = (sat - topos).at(_times(ts, t_unix)).altaz()
    return alt.degrees, az.degrees


def _cross(t: np.ndarray, f: np.ndarray, level: float, upward: bool) -> float | None:
    d = f - level
    hit = (d[:-1] < 0) & (d[1:] >= 0) if upward else (d[:-1] >= 0) & (d[1:] < 0)
    idx = np.nonzero(hit)[0]
    if not idx.size:
        return None
    j = idx[0]
    return float(t[j] + (t[j + 1] - t[j]) * (-d[j] / (d[j + 1] - d[j])))


def _vertex(t: np.ndarray, f: np.ndarray) -> tuple[float, float]:
    """(time, value) of the maximum of a regular grid, parabola through the top 3 samples."""
    j = int(np.nanargmax(f))
    if j == 0 or j == len(f) - 1:
        return float(t[j]), float(f[j])
    y0, y1, y2 = f[j - 1], f[j], f[j + 1]
    den = y0 - 2.0 * y1 + y2
    if den >= 0.0:
        return float(t[j]), float(y1)
    s = 0.5 * (y0 - y2) / den
    return float(t[j] + s * (t[1] - t[0])), float(y1 - 0.25 * (y0 - y2) * s)


def _grid(a: float, b: float, step: float) -> np.ndarray:
    return np.linspace(a, b, max(2, int(math.ceil((b - a) / step - 1e-9)) + 1))


def _refine_crossings(sat, topos, ts, t, alt, brackets, lim):
    """brackets: [(i, upward)] with the crossing between samples i and i+1. Returns the times."""
    grids = [_grid(float(t[i]), float(t[i + 1]), REFINE_S) for i, _ in brackets]
    if not grids:
        return []
    flat = np.concatenate(grids)
    fa, _ = _altaz(sat, topos, ts, flat)
    out, k = [], 0
    for (i, up), g in zip(brackets, grids, strict=True):
        a = fa[k : k + len(g)]
        k += len(g)
        x = _cross(g, a, lim, up)
        out.append(x if x is not None else float((t[i] + t[i + 1]) / 2.0))
    return out


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(first, last) index of every run of True."""
    if not mask.any():
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(
        zip(np.nonzero(d == 1)[0].tolist(), (np.nonzero(d == -1)[0] - 1).tolist(), strict=True)
    )


def _visible(sat, topos, ts, eph, intervals, sun_alt_max):
    """Per interval [c0, c1] the sub-intervals sunlit and Sun alt < sun_alt_max."""
    earth, sun = eph["earth"], eph["sun"]
    observer = earth + topos

    def cond(tu: np.ndarray) -> np.ndarray:
        tt = _times(ts, tu)
        lit = np.asarray(sat.at(tt).is_sunlit(eph), dtype=bool)
        salt = observer.at(tt).observe(sun).apparent().altaz()[0].degrees
        return lit & (salt < sun_alt_max)

    grids = [_grid(c0, c1, COARSE_S) for c0, c1 in intervals]
    flat = np.concatenate(grids) if grids else np.empty(0)
    state = cond(flat) if flat.size else np.empty(0, dtype=bool)
    # transitions between consecutive samples, refined on a 1 s grid in one batch
    trans, k = [], 0
    pieces = []
    for g in grids:
        s = state[k : k + len(g)]
        k += len(g)
        pieces.append(s)
        for j in np.nonzero(s[:-1] != s[1:])[0]:
            trans.append(_grid(float(g[j]), float(g[j + 1]), VIS_REFINE_S))
    tflat = np.concatenate(trans) if trans else np.empty(0)
    tstate = cond(tflat) if tflat.size else np.empty(0, dtype=bool)
    bound: list[float] = []
    k = 0
    for g in trans:
        s = tstate[k : k + len(g)]
        k += len(g)
        ch = np.nonzero(s != s[0])[0]
        j = int(ch[0]) if ch.size else len(g) - 1
        bound.append(float((g[j - 1] + g[j]) / 2.0))
    out, b = [], 0
    for (c0, c1), s in zip(intervals, pieces, strict=True):
        edges: list[float] = []
        cur = bool(s[0])
        if cur:
            edges.append(c0)
        for _ in np.nonzero(s[:-1] != s[1:])[0]:
            edges.append(bound[b])
            b += 1
            cur = not cur
        if cur:
            edges.append(c1)
        out.append([[edges[i], edges[i + 1]] for i in range(0, len(edges), 2)])
    return out


def compute_passes(
    sat: Any,
    topos: Any,
    ts: Any,
    eph: Any,
    lim_el: float,
    start: float,
    end: float,
    mode: str = "los",
    sun_alt_max: float = 0.0,
) -> list[dict]:
    """`sat` is a skyfield EarthSatellite, `topos` a wgs84 site, times are Unix seconds."""
    p0, p1 = start - PAD_S, end + PAD_S
    t = np.arange(p0, p1 + COARSE_S / 2, COARSE_S)
    alt, _ = _altaz(sat, topos, ts, t)
    above = np.nan_to_num(alt, nan=-90.0) >= lim_el
    runs = _runs(above)

    brackets: list[tuple[int, bool]] = []
    for a, b in runs:
        if a > 0:
            brackets.append((a - 1, True))
        if b < len(t) - 1:
            brackets.append((b, False))
    xs = iter(_refine_crossings(sat, topos, ts, t, alt, brackets, lim_el))

    segs = []
    for a, b in runs:
        t_rise = next(xs) if a > 0 else None
        t_set = next(xs) if b < len(t) - 1 else None
        if (t_rise if t_rise is not None else -math.inf) >= end:
            continue
        if (t_set if t_set is not None else math.inf) <= start:
            continue
        segs.append((a, b, t_rise, t_set))

    # peaks: 0.5 s grid +-COARSE_S around the best coarse sample
    pk_grids = []
    for a, b, _, _ in segs:
        j = a + int(np.argmax(alt[a : b + 1]))
        lo, hi = float(t[max(j - 1, 0)]), float(t[min(j + 1, len(t) - 1)])
        pk_grids.append(_grid(lo, hi, REFINE_S))
    azq = [x for _, _, r, s in segs for x in (r, s) if x is not None]
    flat = np.concatenate(pk_grids + [np.array(azq)]) if (pk_grids or azq) else np.empty(0)
    fa, faz = _altaz(sat, topos, ts, flat) if flat.size else (np.empty(0), np.empty(0))
    k = 0
    peaks = []
    for g in pk_grids:
        peaks.append(_vertex(g, fa[k : k + len(g)]))
        k += len(g)
    az_of = dict(zip(azq, faz[k:].tolist(), strict=True)) if azq else {}

    rows = []
    for (_a, _b, t_rise, t_set), (t_peak, max_el) in zip(segs, peaks, strict=True):
        c0 = max(t_rise if t_rise is not None else start, start)
        c1 = min(t_set if t_set is not None else end, end)
        rows.append(
            {
                "t_rise": t_rise,
                "t_set": t_set,
                "rise_az": az_of.get(t_rise) if t_rise is not None else None,
                "set_az": az_of.get(t_set) if t_set is not None else None,
                "t_peak": t_peak,
                "max_el": max_el,
                "duration_s": max(0.0, c1 - c0),
                "_c": (c0, c1),
            }
        )

    if mode == "optical" and rows:
        vis = _visible(sat, topos, ts, eph, [r["_c"] for r in rows], sun_alt_max)
        for r, v in zip(rows, vis, strict=True):
            r["visible"] = v
            r["visible_s"] = float(sum(y - x for x, y in v))
        rows = [r for r in rows if r["visible_s"] > 0.0]
    for r in rows:
        del r["_c"]
    rows.sort(key=lambda r: r["t_rise"] if r["t_rise"] is not None else -math.inf)
    return rows
