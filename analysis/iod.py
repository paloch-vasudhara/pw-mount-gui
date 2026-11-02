"""Angles-only Initial Orbit Determination "lite" (T5.3a; logic sheet A2b-ii, decision O6).

Pure numpy + skyfield + sgp4 (decision O1: no orekit / Java).

Pipeline for three (t, RA, Dec) observations from one site:
  1. site GCRS positions (skyfield `wgs84` `.at(t)`, km);
  2. Gauss (Curtis Alg. 5.5: 8th-order polynomial in |r2|), then the Alg. 5.6 improvement with
     exact f / g from universal variables, until the slant ranges settle (< 1e-8 relative);
  3. velocity at the middle observation: Herrick-Gibbs when both consecutive position vectors are
     < 3 deg apart, else Gibbs (O6);
  4. classical elements, and a coarse TLE: the mean elements (n, e, i, RAAN, argp, M) are fitted
     with SGP4 (damped Newton / least squares) so SGP4 reproduces the P2 state at its epoch.

Input directions are topocentric J2000 RA/Dec (astrometric, as a plate solve gives them). The
~20 arcsec aberration / light-time difference is ignored; it is negligible for a coarse IOD.
Frames: positions / velocities are GCRS (~J2000) in km, km/s; the fit works in TEME.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
from sgp4.api import WGS72, Satrec
from skyfield.api import wgs84
from skyfield.sgp4lib import TEME

from . import state as state_mod

MU = 398600.4418  # km^3/s^2 (the standard Earth value Curtis uses)
RE = 6378.137  # km
R_MIN_ORBIT = 6478.0  # km: "above the atmosphere" root-selection threshold
HG_ANGLE_DEG = 3.0  # O6
MAX_ITER = 50
RHO_TOL = 1e-8
CATNR = "A9999"

MSG_THREE = "Three observations with different epochs are needed"
MSG_NOROOT = "No orbit fits these observations (check the epochs and angles)"
MSG_DIVERGED = "Orbit solution did not converge"
MSG_UNBOUND = "The fitted orbit is not a bound orbit above the Earth"
MSG_DEGENERATE = "Observation directions are too close together"


class IodError(Exception):
    """A failure with a user-readable message (no internals)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------------- geometry helpers
def los_unit(ra_deg: float, dec_deg: float) -> np.ndarray:
    a, d = math.radians(ra_deg), math.radians(dec_deg)
    return np.array([math.cos(d) * math.cos(a), math.cos(d) * math.sin(a), math.sin(d)])


def _skyfield_time(t_unix: float):
    state_mod.warm()
    # Unix time has no leap seconds: go through a UTC datetime (see state.compute_state)
    return state_mod._ts.from_datetime(datetime.fromtimestamp(t_unix, tz=UTC))


def site_gcrs_km(lat: float, lon: float, alt_m: float, t_unix: float) -> np.ndarray:
    return np.asarray(wgs84.latlon(lat, lon, alt_m).at(_skyfield_time(t_unix)).position.km)


def gcrs_to_teme(vec: np.ndarray, t_unix: float) -> np.ndarray:
    """Rotate a GCRS vector into TEME (inertial frame: the same matrix serves r and v)."""
    return np.asarray(TEME.rotation_at(_skyfield_time(t_unix))) @ vec


def _stumpff(z: float) -> tuple[float, float]:
    if abs(z) < 1e-6:
        return 0.5 - z / 24.0, 1.0 / 6.0 - z / 120.0
    if z > 0:
        s = math.sqrt(z)
        return (1 - math.cos(s)) / z, (s - math.sin(s)) / s**3
    s = math.sqrt(-z)
    return (math.cosh(s) - 1) / -z, (math.sinh(s) - s) / s**3


def fg_universal(r0: np.ndarray, v0: np.ndarray, dt: float) -> tuple[float, float]:
    """Lagrange f, g for a time `dt` (s) from (r0, v0) on a Kepler orbit (Curtis Alg. 3.3 / 3.4)."""
    r = float(np.linalg.norm(r0))
    vr = float(r0 @ v0) / r
    alpha = 2.0 / r - float(v0 @ v0) / MU
    sq = math.sqrt(MU)
    chi = sq * abs(alpha) * dt
    for _ in range(MAX_ITER):
        z = alpha * chi * chi
        c, s = _stumpff(z)
        f = r * vr / sq * chi * chi * c + (1 - alpha * r) * chi**3 * s + r * chi - sq * dt
        df = r * vr / sq * chi * (1 - z * s) + (1 - alpha * r) * chi * chi * c + r
        if not math.isfinite(f) or df == 0:
            raise IodError(MSG_DIVERGED)
        step = f / df
        chi -= step
        if abs(step) < 1e-8 * max(1.0, abs(chi)):
            break
    else:
        raise IodError(MSG_DIVERGED)
    z = alpha * chi * chi
    c, s = _stumpff(z)
    return 1 - chi * chi / r * c, dt - chi**3 * s / sq


# --------------------------------------------------------------------------- Gauss + improvement
def _gauss_candidates(rho_hat, R, tau1, tau3):
    """Positive real roots of the Curtis 5.5 polynomial with the Gauss-step state per root."""
    tau = tau3 - tau1
    p1 = np.cross(rho_hat[1], rho_hat[2])
    p2 = np.cross(rho_hat[0], rho_hat[2])
    p3 = np.cross(rho_hat[0], rho_hat[1])
    d0 = float(rho_hat[0] @ p1)
    if abs(d0) < 1e-10:
        raise IodError(MSG_DEGENERATE)
    D = np.array([[R[i] @ p for p in (p1, p2, p3)] for i in range(3)])  # D[i][j] = R_i . p_j
    A = (-D[0, 1] * tau3 / tau + D[1, 1] + D[2, 1] * tau1 / tau) / d0
    B = (D[0, 1] * (tau3**2 - tau**2) * tau3 / tau + D[2, 1] * (tau**2 - tau1**2) * tau1 / tau) / (
        6 * d0
    )
    E = float(R[1] @ rho_hat[1])
    R22 = float(R[1] @ R[1])
    a = -(A * A + 2 * A * E + R22)
    b = -2 * MU * B * (A + E)
    c = -(MU**2) * B * B
    roots = np.roots([1, 0, a, 0, 0, b, 0, 0, c])
    out = []
    for z in roots:
        if abs(z.imag) <= 1e-7 * max(1.0, abs(z.real)) and z.real > 0:
            x = float(z.real)
            x3 = x**3
            rho1 = (
                (6 * (D[2, 0] * tau1 / tau3 + D[1, 0] * tau / tau3) * x3
                 + MU * D[2, 0] * (tau**2 - tau1**2) * tau1 / tau3)
                / (6 * x3 + MU * (tau**2 - tau3**2))
                - D[0, 0]
            ) / d0
            rho2 = A + MU * B / x3
            rho3 = (
                (6 * (D[0, 2] * tau3 / tau1 - D[1, 2] * tau / tau1) * x3
                 + MU * D[0, 2] * (tau**2 - tau3**2) * tau3 / tau1)
                / (6 * x3 + MU * (tau**2 - tau1**2))
                - D[2, 2]
            ) / d0
            rho = np.array([rho1, rho2, rho3])
            if np.all(np.isfinite(rho)) and np.all(rho > 0):
                out.append((x, rho))
    return out, D, d0


def _state_from_rho(rho, rho_hat, R, f1, g1, f3, g3):
    r = R + rho[:, None] * rho_hat
    den = f1 * g3 - f3 * g1
    v2 = (-f3 * r[0] + f1 * r[2]) / den
    return r, v2


def _gauss_state(rho, rho_hat, R, tau1, tau3, x):
    x3 = x**3
    f1 = 1 - MU * tau1**2 / (2 * x3)
    f3 = 1 - MU * tau3**2 / (2 * x3)
    g1 = tau1 - MU * tau1**3 / (6 * x3)
    g3 = tau3 - MU * tau3**3 / (6 * x3)
    r, v2 = _state_from_rho(rho, rho_hat, R, f1, g1, f3, g3)
    return r[1], v2


def _improve(rho0, rho_hat, R, D, d0, tau1, tau3, x):
    """Curtis Alg. 5.6 starting from the Gauss slant ranges; returns (r[3x3], v2, iterations)."""
    x3 = x**3
    f1 = 1 - MU * tau1**2 / (2 * x3)
    f3 = 1 - MU * tau3**2 / (2 * x3)
    g1 = tau1 - MU * tau1**3 / (6 * x3)
    g3 = tau3 - MU * tau3**3 / (6 * x3)
    rho = rho0.copy()
    r, v2 = _state_from_rho(rho, rho_hat, R, f1, g1, f3, g3)
    for it in range(1, MAX_ITER + 1):
        r2 = r[1]
        f1, g1 = fg_universal(r2, v2, tau1)
        f3, g3 = fg_universal(r2, v2, tau3)
        den = f1 * g3 - f3 * g1
        if not math.isfinite(den) or abs(den) < 1e-12:
            raise IodError(MSG_DIVERGED)
        c1, c3 = g3 / den, -g1 / den
        new = np.array(
            [
                (-D[0, 0] + D[1, 0] / c1 - c3 / c1 * D[2, 0]) / d0,
                (-c1 * D[0, 1] + D[1, 1] - c3 * D[2, 1]) / d0,
                (-c1 / c3 * D[0, 2] + D[1, 2] / c3 - D[2, 2]) / d0,
            ]
        )
        if not np.all(np.isfinite(new)) or np.any(new <= 0):
            raise IodError(MSG_DIVERGED)
        r, v2 = _state_from_rho(new, rho_hat, R, f1, g1, f3, g3)
        done = float(np.max(np.abs(new - rho) / new)) < RHO_TOL
        rho = new
        if done:
            return r, v2, it
    raise IodError(MSG_DIVERGED)


def _los_residual(y, rho_hat, R, tau1, tau3):
    """Off-axis components of (r_i - R_i) w.r.t. the observed directions, for state y=(r2, v2)."""
    r2, v2 = y[:3], y[3:]
    res = []
    for k, tau in ((0, tau1), (1, 0.0), (2, tau3)):
        if tau == 0.0:
            ri = r2
        else:
            f, g = fg_universal(r2, v2, tau)
            ri = f * r2 + g * v2
        d = ri - R[k]
        n = float(np.linalg.norm(d))
        if n == 0 or not math.isfinite(n):
            raise IodError(MSG_DIVERGED)
        u = d / n
        res.append(u - rho_hat[k] * float(u @ rho_hat[k]))
    return np.concatenate(res)


def _refine_lm(r2, v2, rho_hat, R, tau1, tau3):
    """Levenberg-Marquardt on the 6-dof state (r2, v2) so that the Kepler-propagated positions at
    t1 / t3 lie on the observed lines of sight. Fallback for arcs where the fixed-point Alg. 5.6
    drifts (long arcs: MEO / GEO), started from the Gauss state.
    Returns (r[3x3], v2, iterations)."""
    y = np.concatenate([r2, v2])
    scale = np.concatenate(
        [np.full(3, 1e-5 * np.linalg.norm(r2)), np.full(3, 1e-5 * np.linalg.norm(v2))]
    )
    res = _los_residual(y, rho_hat, R, tau1, tau3)
    cost = float(res @ res)
    lam = 1e-3
    for it in range(1, 61):
        jac = np.empty((9, 6))
        for k in range(6):
            dy = np.zeros(6)
            dy[k] = scale[k]
            jac[:, k] = (
                _los_residual(y + dy, rho_hat, R, tau1, tau3)
                - _los_residual(y - dy, rho_hat, R, tau1, tau3)
            ) / (2 * scale[k])
        jtj, jtr = jac.T @ jac, jac.T @ res
        for _ in range(12):
            try:
                step = np.linalg.solve(jtj + lam * np.diag(np.diag(jtj) + 1e-30), -jtr)
                new = _los_residual(y + step, rho_hat, R, tau1, tau3)
            except (np.linalg.LinAlgError, IodError):
                lam *= 10
                continue
            nc = float(new @ new)
            if nc < cost:
                y, res, cost, lam = y + step, new, nc, max(lam / 10, 1e-12)
                break
            lam *= 10
        else:
            raise IodError(MSG_DIVERGED)
        if math.sqrt(cost) < 1e-11 or float(np.linalg.norm(step / scale)) < 1e-6:
            r2n, v2n = y[:3], y[3:]
            f1, g1 = fg_universal(r2n, v2n, tau1)
            f3, g3 = fg_universal(r2n, v2n, tau3)
            return np.array([f1 * r2n + g1 * v2n, r2n, f3 * r2n + g3 * v2n]), v2n, it
    raise IodError(MSG_DIVERGED)


# --------------------------------------------------------------------------- Gibbs / Herrick-Gibbs
def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    c = float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def gibbs_v2(r1, r2, r3) -> np.ndarray:
    n1, n2, n3 = (float(np.linalg.norm(x)) for x in (r1, r2, r3))
    c12, c23, c31 = np.cross(r1, r2), np.cross(r2, r3), np.cross(r3, r1)
    d = c12 + c23 + c31
    n = n1 * c23 + n2 * c31 + n3 * c12
    s = (n2 - n3) * r1 + (n3 - n1) * r2 + (n1 - n2) * r3
    dn = float(n @ d)
    if dn <= 0:
        raise IodError(MSG_DIVERGED)
    return math.sqrt(MU / dn) * (np.cross(d, r2) / n2 + s)


def herrick_gibbs_v2(r1, r2, r3, t1, t2, t3) -> np.ndarray:
    d21, d32, d31 = t2 - t1, t3 - t2, t3 - t1
    n1, n2, n3 = (float(np.linalg.norm(x)) for x in (r1, r2, r3))
    return (
        -d32 * (1 / (d21 * d31) + MU / (12 * n1**3)) * r1
        + (d32 - d21) * (1 / (d21 * d32) + MU / (12 * n2**3)) * r2
        + d21 * (1 / (d32 * d31) + MU / (12 * n3**3)) * r3
    )


# --------------------------------------------------------------------------- elements
def coe_from_rv(r: np.ndarray, v: np.ndarray) -> dict:
    """Classical elements from a state; raises IodError unless a bound orbit above the Earth."""
    rn, vn = float(np.linalg.norm(r)), float(np.linalg.norm(v))
    h = np.cross(r, v)
    hn = float(np.linalg.norm(h))
    energy = vn * vn / 2 - MU / rn
    if not (math.isfinite(energy) and energy < 0 and hn > 0):
        raise IodError(MSG_UNBOUND)
    a = -MU / (2 * energy)
    evec = (np.cross(v, h) / MU) - r / rn
    e = float(np.linalg.norm(evec))
    if e >= 1 or a * (1 - e) < RE:
        raise IodError(MSG_UNBOUND)
    inc = math.acos(max(-1.0, min(1.0, h[2] / hn)))
    node = np.cross([0.0, 0.0, 1.0], h)
    nn = float(np.linalg.norm(node))
    if nn > 1e-9 * hn:
        raan = math.atan2(node[1], node[0]) % (2 * math.pi)
    else:
        raan = 0.0
    if e > 1e-9:
        if nn > 1e-9 * hn:
            argp = math.acos(max(-1.0, min(1.0, float(node @ evec) / (nn * e))))
            if evec[2] < 0:
                argp = 2 * math.pi - argp
        else:
            argp = math.atan2(evec[1], evec[0]) % (2 * math.pi)
            if h[2] < 0:
                argp = (2 * math.pi - argp) % (2 * math.pi)
        nu = math.acos(max(-1.0, min(1.0, float(evec @ r) / (e * rn))))
        if float(r @ v) < 0:
            nu = 2 * math.pi - nu
    else:  # circular: measure from the node (or x axis)
        argp = 0.0
        ref = node / nn if nn > 1e-9 * hn else np.array([1.0, 0.0, 0.0])
        nu = math.acos(max(-1.0, min(1.0, float(ref @ r) / rn)))
        if (r[2] < 0) if nn > 1e-9 * hn else (np.cross(ref, r)[2] * np.sign(h[2]) < 0):
            nu = 2 * math.pi - nu
    return {
        "a_km": a,
        "e": e,
        "i_deg": math.degrees(inc),
        "raan_deg": math.degrees(raan),
        "argp_deg": math.degrees(argp),
        "nu_deg": math.degrees(nu),
        "period_min": 2 * math.pi * math.sqrt(a**3 / MU) / 60.0,
    }


def _mean_anomaly(e: float, nu_rad: float) -> float:
    ea = 2 * math.atan2(
        math.sqrt(1 - e) * math.sin(nu_rad / 2), math.sqrt(1 + e) * math.cos(nu_rad / 2)
    )
    return (ea - e * math.sin(ea)) % (2 * math.pi)


# --------------------------------------------------------------------------- SGP4-fitted TLE
def _checksum(line: str) -> int:
    return sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line) % 10


class _Epoch:
    """TLE epoch quantised to the format's 1e-8 day; `units` = days * 1e8 since 1 Jan 00:00."""

    def __init__(self, t_unix: float):
        dt = datetime.fromtimestamp(t_unix, tz=UTC)
        ys = datetime(dt.year, 1, 1, tzinfo=UTC).timestamp()
        self.year = dt.year
        self.units = round((t_unix - ys) / 86400.0 * 1e8)
        self.t_unix = ys + self.units * 86400.0 / 1e8
        self.days1949 = (datetime(dt.year, 1, 1) - datetime(1949, 12, 31)).days + self.units / 1e8

    def field(self) -> str:
        return f"{self.year % 100:02d}{1 + self.units / 1e8:012.8f}"


def _sgp4_state(x, days1949: float):
    """TEME (r, v) at the epoch for mean elements x = (n rad/min, e, i, raan, argp, M) rad."""
    sat = Satrec()
    try:
        sat.sgp4init(WGS72, "i", 9999, days1949, 0.0, 0.0, 0.0, x[1], x[4], x[2], x[5], x[0], x[3])
        err, r, v = sat.sgp4_tsince(0.0)
    except Exception:
        return None
    if err != 0 or not (np.all(np.isfinite(r)) and np.all(np.isfinite(v))):
        return None
    return np.array(r), np.array(v)


# TLE field resolutions: n [rev/day] 1e-8, e 1e-7, angles 1e-4 deg
_LSB = np.array(
    [1e-8 * 2 * math.pi / 1440.0, 1e-7] + [math.radians(1e-4)] * 4
)
_POS_W, _VEL_W = 0.01, 1e-5  # 10 m <-> 1 cm/s residual weights


def _quantise(x) -> np.ndarray:
    return np.round(np.asarray(x) / _LSB) * _LSB


def _residual(x, days1949, r_t, v_t):
    s = _sgp4_state(x, days1949)
    if s is None:
        return None
    return np.concatenate([(s[0] - r_t) / _POS_W, (s[1] - v_t) / _VEL_W])


def _format_tle(x, ep: _Epoch) -> tuple[str, str]:
    n_rev = x[0] * 1440.0 / (2 * math.pi)
    i, raan, argp, m = (math.degrees(x[k]) % 360.0 for k in (2, 3, 4, 5))
    ecc = int(round(x[1] * 1e7))
    # rounding to 4 decimals must not reach 360.0000
    i, raan, argp, m = (round(a, 4) % 360.0 for a in (i, raan, argp, m))
    l1 = (
        f"1 {CATNR}U {' ' * 8} {ep.field()} {' .00000000'} {' 00000-0'} {' 00000-0'} 0  999"
    )
    l2 = f"2 {CATNR} {i:8.4f} {raan:8.4f} {ecc:07d} {argp:8.4f} {m:8.4f} {n_rev:11.8f}{1:5d}"
    return l1 + str(_checksum(l1)), l2 + str(_checksum(l2))


def _tle_error(lines, t2_unix: float, r_teme, v_teme):
    """Position (km) / velocity (m/s) error of the emitted TLE at t2 vs the P2 TEME state."""
    sat = Satrec.twoline2rv(lines[0], lines[1])
    d = t2_unix / 86400.0
    err, r, v = sat.sgp4(2440587.5 + math.floor(d), d - math.floor(d))
    if err != 0:
        return float("inf"), float("inf")
    return (
        float(np.linalg.norm(np.array(r) - r_teme)),
        float(np.linalg.norm(np.array(v) - v_teme)) * 1000.0,
    )


def fit_tle(r_teme: np.ndarray, v_teme: np.ndarray, t2_unix: float, coe: dict) -> dict:
    """SGP4-fitted coarse TLE at t2. Returns {line1, line2, tle_fit{...}}."""
    ep = _Epoch(t2_unix)
    dts = ep.t_unix - t2_unix  # <= 0.43 ms: move the target state to the quantised epoch
    acc = -MU * r_teme / float(np.linalg.norm(r_teme)) ** 3
    r_t = r_teme + v_teme * dts + acc * dts * dts / 2
    v_t = v_teme + acc * dts

    e0 = coe["e"]
    n0 = 2 * math.pi / (coe["period_min"])  # rad/min
    x = np.array(
        [
            n0,
            max(e0, 1e-7),
            math.radians(coe["i_deg"]),
            math.radians(coe["raan_deg"]),
            math.radians(coe["argp_deg"]),
            _mean_anomaly(e0, math.radians(coe["nu_deg"])),
        ]
    )
    osc_x = x.copy()
    steps = np.array([1e-9 * n0 * 1e3, 1e-7, 1e-6, 1e-6, 1e-6, 1e-6])
    res = _residual(x, ep.days1949, r_t, v_t)
    iters = 0
    if res is not None:
        for iters in range(1, 11):
            if np.max(np.abs(res[:3])) * _POS_W < 1e-3 and np.max(np.abs(res[3:])) * _VEL_W < 1e-6:
                iters -= 1
                break
            jac = np.zeros((6, 6))
            ok = True
            for k in range(6):
                dx = np.zeros(6)
                dx[k] = steps[k]
                rp = _residual(x + dx, ep.days1949, r_t, v_t)
                rm = _residual(x - dx, ep.days1949, r_t, v_t)
                if rp is None or rm is None:
                    ok = False
                    break
                jac[:, k] = (rp - rm) / (2 * steps[k])
            if not ok:
                break
            dx = np.linalg.lstsq(jac, -res, rcond=1e-12)[0]
            lam, improved = 1.0, False
            for _ in range(6):
                xn = x + lam * dx
                xn[1] = max(xn[1], 1e-7)
                rn = _residual(xn, ep.days1949, r_t, v_t)
                if rn is not None and np.linalg.norm(rn) < np.linalg.norm(res):
                    x, res, improved = xn, rn, True
                    break
                lam /= 2
            if not improved:
                break
    fit_pos = float(np.linalg.norm(res[:3])) * _POS_W if res is not None else float("inf")
    fit_vel = float(np.linalg.norm(res[3:])) * _VEL_W if res is not None else float("inf")
    converged = fit_pos <= 0.01 and fit_vel <= 1e-5  # <= 10 m, <= 1 cm/s

    use = x if converged else osc_x
    q = _quantise(use)
    if converged:  # polish on the TLE lattice: try +-1 LSB on every field, twice
        best = q.copy()
        rb = _residual(best, ep.days1949, r_t, v_t)
        best_cost = float(np.linalg.norm(rb)) if rb is not None else float("inf")
        for _ in range(2):
            cur = best.copy()
            for combo in np.ndindex(3, 3, 3, 3, 3, 3):
                if combo == (1, 1, 1, 1, 1, 1):
                    continue
                cand = cur + (np.array(combo) - 1) * _LSB
                rc = _residual(cand, ep.days1949, r_t, v_t)
                if rc is not None:
                    cost = float(np.linalg.norm(rc))
                    if cost < best_cost:
                        best, best_cost = cand, cost
            if np.array_equal(best, cur):
                break
        q = best
    l1, l2 = _format_tle(q, ep)
    pos_err, vel_err = _tle_error((l1, l2), t2_unix, r_teme, v_teme)
    return {
        "line1": l1,
        "line2": l2,
        "tle_fit": {
            "iterations": int(iters),
            "pos_err_km": pos_err,
            "vel_err_ms": vel_err,
            "converged": bool(converged),
        },
    }


# --------------------------------------------------------------------------- top level
def solve(site: dict, obs: list[dict]) -> dict:
    """site {lat, lon, alt_m}; obs [{t_unix, ra_deg, dec_deg}] x 3 (any order). See module doc."""
    if len(obs) != 3:
        raise IodError(MSG_THREE)
    rows = sorted(obs, key=lambda o: o["t_unix"])
    t = np.array([o["t_unix"] for o in rows], dtype=float)
    if not np.all(np.isfinite(t)) or t[1] - t[0] < 1e-3 or t[2] - t[1] < 1e-3:
        raise IodError(MSG_THREE)
    rho_hat = np.array([los_unit(o["ra_deg"], o["dec_deg"]) for o in rows])
    for a, b in ((0, 1), (1, 2), (0, 2)):
        if _angle_deg(rho_hat[a], rho_hat[b]) < 1e-5:  # ~0.04 arcsec
            raise IodError(MSG_DEGENERATE)
    R = np.array([site_gcrs_km(site["lat"], site["lon"], site["alt_m"], ti) for ti in t])
    tau1, tau3 = float(t[0] - t[1]), float(t[2] - t[1])

    with np.errstate(all="ignore"):
        return _solve_inner(rho_hat, R, t, tau1, tau3)


def _solve_inner(rho_hat, R, t, tau1, tau3):
    cands, D, d0 = _gauss_candidates(rho_hat, R, tau1, tau3)
    if not cands:
        raise IodError(MSG_NOROOT)
    high = [c for c in cands if c[0] > R_MIN_ORBIT]
    if not high:
        raise IodError(MSG_UNBOUND)
    high.sort(key=lambda c: c[0])
    last: IodError | None = None
    for x, rho0 in high:
        try:
            try:
                r, v2, iters = _improve(rho0, rho_hat, R, D, d0, tau1, tau3, x)
            except IodError:  # long arcs: the fixed point drifts; refine the Gauss state instead
                g_r, g_v = _gauss_state(rho0, rho_hat, R, tau1, tau3, x)
                r, v2, iters = _refine_lm(g_r, g_v, rho_hat, R, tau1, tau3)
            r1, r2, r3 = r
            ang = max(_angle_deg(r1, r2), _angle_deg(r2, r3))
            if ang < HG_ANGLE_DEG:
                v2 = herrick_gibbs_v2(r1, r2, r3, t[0], t[1], t[2])
                method = "gauss+herrick-gibbs"
            else:
                v2 = gibbs_v2(r1, r2, r3)
                method = "gauss+gibbs"
            coe = coe_from_rv(r2, v2)
        except IodError as e:
            last = e
            continue
        except (OverflowError, ZeroDivisionError, FloatingPointError, ValueError):
            last = IodError(MSG_DIVERGED)
            continue
        t2 = float(t[1])
        tle = fit_tle(gcrs_to_teme(r2, t2), gcrs_to_teme(v2, t2), t2, coe)
        return {
            "epoch_unix": t2,
            "r_km": [float(c) for c in r2],
            "v_kms": [float(c) for c in v2],
            "method": method,
            "iterations": int(iters),
            "coe": {k: float(val) for k, val in coe.items()},
            "tle": {"line1": tle["line1"], "line2": tle["line2"]},
            "tle_fit": tle["tle_fit"],
            "roots": {"count": len(high), "r2_gauss_km": float(x)},
        }
    raise last or IodError(MSG_NOROOT)
