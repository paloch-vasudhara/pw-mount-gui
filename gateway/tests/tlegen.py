"""Deterministic synthetic LEO TLE generator (T4.2a probe, T4.2b tests; reused by T4.2c).

No wall clock: the epoch is an argument.

gen_tle(peak_target_deg, lead_s) -> dict(line1, line2, name, t_in, t_out, t_peak, peak, ...)
The pass over the site rises so that the upward 15 deg crossing is `lead_s` after the epoch (now).
"""

from __future__ import annotations

import math

import numpy as np
from sgp4.api import WGS72, Satrec
from sgp4.exporter import export_tle

INC = 51.6
ECC = 0.0005
ARGP = 90.0
N_REV = 15.5
N_DEG_S = N_REV * 360.0 / 86400.0
LIM = 15.0
A_WGS, F_WGS = 6378.137, 1 / 298.257223563


def observer_ecef(lat, lon, h_m):
    la, lo = math.radians(lat), math.radians(lon)
    e2 = F_WGS * (2 - F_WGS)
    N = A_WGS / math.sqrt(1 - e2 * math.sin(la) ** 2)
    h = h_m / 1000.0
    return np.array(
        [
            (N + h) * math.cos(la) * math.cos(lo),
            (N + h) * math.cos(la) * math.sin(lo),
            (N * (1 - e2) + h) * math.sin(la),
        ]
    )


def make_sat(raan, M, epoch_unix, satnum=99001, bstar=1e-4):
    s = Satrec()
    jd = epoch_unix / 86400.0 + 2440587.5
    s.sgp4init(
        WGS72,
        "i",
        satnum,
        jd - 2433281.5,
        bstar,
        0.0,
        0.0,
        ECC,
        math.radians(ARGP),
        math.radians(INC),
        math.radians(M % 360.0),
        N_REV * 2 * math.pi / 1440.0,
        math.radians(raan % 360.0),
    )
    return s


def alt_az_fast(sat, epoch_unix, ts_off, site):
    """Geometric alt/az (deg) at epoch+ts_off seconds (array). GMST rotation only."""
    lat, lon, h = site
    t = epoch_unix + ts_off
    jd = t / 86400.0 + 2440587.5
    whole = np.floor(jd - 0.5) + 0.5
    fr = jd - whole
    e, r, _ = sat.sgp4_array(whole, fr)
    gmst = np.radians((280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0)
    c, s_ = np.cos(gmst), np.sin(gmst)
    x = r[:, 0] * c + r[:, 1] * s_
    y = -r[:, 0] * s_ + r[:, 1] * c
    d = np.stack([x, y, r[:, 2]], axis=1) - observer_ecef(lat, lon, h)
    la, lo = math.radians(lat), math.radians(lon)
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    rng = np.linalg.norm(d, axis=1)
    alt = np.degrees(np.arcsin(np.clip(d @ up / rng, -1, 1)))
    az = np.degrees(np.arctan2(d @ east, d @ north)) % 360.0
    return alt, az


def _cross(t, a, level):
    idx = np.nonzero((a[:-1] < level) & (a[1:] >= level))[0]
    if not idx.size:
        return None
    k = idx[0]
    return t[k] + (level - a[k]) / (a[k + 1] - a[k]) * (t[k + 1] - t[k])


def metrics(sat, epoch, site, step=1.0, span=900.0):
    t = np.arange(0.0, span, step)
    alt, _ = alt_az_fast(sat, epoch, t, site)
    if alt[0] >= 0:  # already up: skip to next rise
        pass
    t_in = _cross(t, alt, LIM)
    if t_in is None:
        return None
    t_rise = _cross(t, alt, 0.0)
    # contiguous pass peak only: stop at first descent below 0 after t_in
    end = np.nonzero(alt[np.searchsorted(t, t_in) :] < 0)[0]
    kend = np.searchsorted(t, t_in) + (end[0] if end.size else len(t) - np.searchsorted(t, t_in))
    seg = alt[np.searchsorted(t, t_in) : kend]
    kp = int(np.argmax(seg)) + np.searchsorted(t, t_in)
    peak = float(alt[kp])
    if 0 < kp < len(t) - 1:
        a, b, c = alt[kp - 1], alt[kp], alt[kp + 1]
        den = a - 2 * b + c
        if den != 0:
            peak = float(b - (a - c) ** 2 / (8 * den))
    return dict(t_in=t_in, t_rise=t_rise, peak=peak, t_peak=float(t[kp]))


def _tune(raan, M, epoch, site, lead):
    """M iteration so that t_in == lead; returns (M, metrics)."""
    m = None
    for _ in range(5):
        sat = make_sat(raan, M, epoch)
        m = metrics(sat, epoch, site)
        if m is None:
            return M, None
        err = m["t_in"] - lead
        if abs(err) < 0.3:
            break
        M = (M + N_DEG_S * err) % 360.0
    return M, m


def gen_tle(peak_target, lead_s, site, epoch, name="SYN-A", satnum=99001, verbose=False):
    # coarse scan on (raan, M)
    best = []
    t = np.arange(0.0, 700.0, 2.0)
    for raan in np.arange(0.0, 360.0, 1.0):
        for M in np.arange(0.0, 360.0, 3.0):
            sat = make_sat(raan, M, epoch)
            alt, _ = alt_az_fast(sat, epoch, t, site)
            if alt[0] > LIM - 1.0:
                continue
            ti = _cross(t, alt, LIM)
            if ti is None or abs(ti - lead_s) > 40:
                continue
            kk = np.searchsorted(t, ti)
            pk = float(alt[kk : kk + 120].max())
            best.append((abs(pk - peak_target), raan, M, pk, ti))
    best.sort()
    if verbose:
        print("coarse cells", len(best), best[:3])
    cands = best[:8]
    pool = []
    for _, raan0, M0, _, _ in cands:
        Mt, m = _tune(raan0, M0, epoch, site, lead_s)
        if m is not None and abs(m["t_in"] - lead_s) < 1.0:
            pool.append((abs(m["peak"] - peak_target), raan0, Mt, m))
    pool.sort(key=lambda x: x[0])
    result = None
    for _, raan0, M0, _ in pool[:3]:
        centre, Mc = raan0, M0
        cur = None
        for span, step in ((1.6, 0.1), (0.12, 0.005), (0.008, 0.0004)):
            grid = np.arange(centre - span, centre + span + 1e-9, step)
            sc = []
            Mk = Mc
            for rr in grid:
                Mt, m = _tune(rr, Mk, epoch, site, lead_s)
                if m is None or abs(m["t_in"] - lead_s) > 1.0:
                    continue
                Mk = Mt
                sc.append((abs(m["peak"] - peak_target), rr, Mt, m))
            if not sc:
                break
            sc.sort(key=lambda x: x[0])
            cur = sc[0]
            centre, Mc = cur[1], cur[2]
        if cur is not None and (result is None or cur[0] < result[0]):
            result = cur
        if result is not None and result[0] < 0.5:
            break
    if result is None or result[0] > 2.0:
        raise RuntimeError(
            "no TLE found (best peak err %s)" % (None if result is None else result[0])
        )
    _, raan, M, m = result
    sat = make_sat(raan, M, epoch, satnum=satnum)
    l1, l2 = export_tle(sat)
    return dict(name=name, line1=l1, line2=l2, epoch=epoch, raan=raan, M=M, fast=m)


# Fixed outputs of gen_tle for the T4.2 tests: site (14.3479, 100.5656, 12.0) (the test_passes
# site), epoch 2026-10-28 12:00 UTC = 1793188800 Unix. In both the first Lim EL (15 deg) crossing
# is ~60 s after the epoch and the peak ~220 s after it.
TEST_SITE = (14.3479, 100.5656, 12.0)
TEST_EPOCH = 1793188800.0
ZENITH_89 = (  # peak 89.0 deg
    "1 99001U          26301.50000000  .00000000  00000-0  10000-3 0    03",
    "2 99001  51.6000 149.8946 0005000  90.0000  57.3845 15.50000000    01",
)
PEAK_70 = (  # peak 70.0 deg
    "1 99001U          26301.50000000  .00000000  00000-0  10000-3 0    03",
    "2 99001  51.6000 304.9966 0005000  90.0000 275.3438 15.50000000    07",
)
