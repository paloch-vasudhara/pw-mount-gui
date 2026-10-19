"""PWI4 simulation-mode scenarios (T0.3). Each returns (findings, raw) and leaves a safe state."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from common import (
    SITE_ELEV_M,
    SITE_LAT,
    SITE_LON,
    Ctx,
    parse_utc,
    sep_deg,
    settled,
    snap,
    stats,
    wrap180,
)
from sgp4.api import WGS72, Satrec
from sgp4.exporter import export_tle
from skyfield.api import EarthSatellite, load, wgs84

ARCSEC = 1.0 / 3600.0
_TS = load.timescale()


def _changed_fields(raw_samples: list[dict[str, str]]) -> list[str]:
    skip = ("timestamp", "julian", "lmst", "response")
    keys = set().union(*[r.keys() for r in raw_samples]) if raw_samples else set()
    out = []
    for k in sorted(keys):
        if any(s in k for s in skip):
            continue
        if len({r.get(k) for r in raw_samples}) > 1:
            out.append(k)
    return out


def _first(samples, pred):
    for s in samples:
        if pred(s):
            return s["t"]
    return None


def _first_after(samples, start_t, pred):
    for s in samples:
        if s["t"] >= start_t and pred(s):
            return s["t"]
    return None


def _moving(s) -> bool:
    return abs(s["v0"] or 0) > 0.02 or abs(s["v1"] or 0) > 0.02


def _jd(dt: datetime) -> float:
    """UTC Julian date from an aware datetime."""
    return dt.timestamp() / 86400.0 + 2440587.5


# ---------------------------------------------------------------- 1 ---
def s01_connect_home(ctx: Ctx):
    f: dict[str, Any] = {}
    pwi = ctx.pwi
    t0 = time.perf_counter()
    pwi.mount_connect()
    f["connect_s"] = round(time.perf_counter() - t0, 3)
    ctx.connected = True
    for i in (0, 1):
        pwi.mount_enable(i)
    st = pwi.status()
    f["pwi4_version"] = st.pwi4.version
    f["axes_enabled"] = [st.mount.axis0.is_enabled, st.mount.axis1.is_enabled]
    f["slew_time_constant"] = st.mount.slew_time_constant
    f["axis0_wrap_range_min_degs"] = st.mount.axis0_wrap_range_min_degs
    f["axis0_vmax"] = st.mount.axis0.max_velocity_degs_per_sec
    f["axis0_accel"] = st.mount.axis0.acceleration_degs_per_sec_sqr
    f["axis0_range"] = [
        st.mount.axis0.min_mech_position_degs,
        st.mount.axis0.max_mech_position_degs,
    ]
    f["axis1_range"] = [
        st.mount.axis1.min_mech_position_degs,
        st.mount.axis1.max_mech_position_degs,
    ]
    f["pos_before_home"] = [st.mount.axis0.position_degs, st.mount.axis1.position_degs]
    raws: list[dict[str, str]] = [dict(st.raw)]
    samples = []
    quiet_since = None
    t0 = time.perf_counter()
    pwi.mount_find_home()
    while time.perf_counter() - t0 < 120:
        r = time.perf_counter()
        st = pwi.status()
        s = ctx._derive(snap(st, r - t0), r)
        samples.append(s)
        raws.append(dict(st.raw))
        if _moving(s) or s["slewing"]:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = s["t"]
        if quiet_since is not None and s["t"] - quiet_since >= 2.0 and s["t"] > 2.0:
            break
        time.sleep(0.1)
    last_moving = max((s["t"] for s in samples if _moving(s)), default=None)
    f["first_motion_s"] = _first(samples, _moving)
    f["homing_done_s"] = last_moving if last_moving is not None else 0.0
    f["total_poll_s"] = round(samples[-1]["t"], 2)
    f["was_slewing_flag_set"] = any(s["slewing"] for s in samples)
    f["fields_that_changed_during_homing"] = _changed_fields(raws)
    f["pos_after_home"] = [samples[-1]["p0"], samples[-1]["p1"]]
    f["az_alt_after_home"] = [samples[-1]["az"], samples[-1]["alt"]]
    ctx.note(
        "Homing: PWI4 has no dedicated 'homing' status field; see "
        "fields_that_changed_during_homing (is_slewing set during homing: "
        f"{f['was_slewing_flag_set']})."
    )
    return f, {"samples": samples}


# ---------------------------------------------------------------- 2 ---
def s02_goto_altaz(ctx: Ctx):
    f: dict[str, Any] = {}
    raw = {}
    for alt, az in ((45.0, 180.0), (30.0, 90.0)):
        key = f"goto_alt{alt:g}_az{az:g}"
        t0 = time.perf_counter()
        ctx.pwi.mount_goto_alt_az(alt, az)
        smp = ctx.poll(60, t0=t0, until=lambda s: s["t"] > 0.5 and settled(s))
        raw[key] = smp
        last = smp[-1]
        first_slew = _first(smp, lambda s: s["slewing"])
        f[key] = {
            "start_az_alt": [round(smp[0]["az"], 3), round(smp[0]["alt"], 3)],
            "slewing_first_true_s": first_slew,
            "slew_flag_clear_s": _first_after(smp, first_slew or 0, lambda s: not s["slewing"]),
            "settled_s": last["t"] if settled(last) else None,
            "final_az_alt": [round(last["az"], 4), round(last["alt"], 4)],
            "err_az_deg": round(wrap180(last["az"] - az), 5),
            "err_alt_deg": round(last["alt"] - alt, 5),
            "tracking_during_slew": any(s["tracking"] for s in smp if s["slewing"]),
            "tracking_after": last["tracking"],
            "max_abs_derived_v0_degs": round(max(abs(s["v0"]) for s in smp), 2),
        }
    return f, raw


# ---------------------------------------------------------------- 3 ---
def s03_stop_midslew(ctx: Ctx):
    f: dict[str, Any] = {}
    ctx.goto_altaz_wait(45.0, 180.0)
    t0 = time.perf_counter()
    ctx.pwi.mount_goto_alt_az(45.0, 0.0)
    pre = ctx.poll(1.5, t0=t0)
    f["velocity_at_stop_degs"] = round(max(abs(pre[-1]["v0"]), abs(pre[-1]["v1"])), 3)
    t_cmd = time.perf_counter() - t0
    ctx.pwi.mount_stop()
    t_ack = time.perf_counter() - t0
    post = ctx.poll(10, t0=t0, until=lambda s: s["t"] > t_ack and settled(s, 0.05))
    f["stop_cmd_http_ms"] = round((t_ack - t_cmd) * 1000, 1)
    f["slew_flag_clear_after_stop_s"] = _first_after(post, t_cmd, lambda s: not s["slewing"])
    f["vel_zero_after_stop_s"] = _first_after(
        post, t_cmd, lambda s: abs(s["v0"] or 0) < 0.05 and abs(s["v1"] or 0) < 0.05
    )
    f["stop_latency_s"] = round(post[-1]["t"] - t_cmd, 3) if settled(post[-1], 0.05) else None
    pos_a = (post[-1]["p0"], post[-1]["p1"])
    time.sleep(1)
    after = ctx.sample()
    f["drift_1s_after_stop_deg"] = round(
        max(abs(after["p0"] - pos_a[0]), abs(after["p1"] - pos_a[1])), 4
    )
    f["az_when_stopped"] = round(post[-1]["az"], 2)
    return f, {"pre": pre, "post": post}


# ---------------------------------------------------------------- 4 ---
def s04_radec_tracking(ctx: Ctx):
    f: dict[str, Any] = {}
    ctx.pwi.mount_tracking_off()
    st = ctx.status()
    ra, dec = st.site.lmst_hours % 24, 30.0
    f["lmst_hours"] = st.site.lmst_hours
    f["target_ra_dec_j2000"] = [round(ra, 4), dec]
    f["tracking_before"] = st.mount.is_tracking
    t0 = time.perf_counter()
    ctx.pwi.mount_goto_ra_dec_j2000(ra, dec)
    smp = ctx.poll(60, t0=t0, until=lambda s: s["t"] > 0.5 and settled(s))
    last = smp[-1]
    f["goto_settled_s"] = last["t"] if settled(last) else None
    f["tracking_after_goto_without_tracking_on"] = last["tracking"]
    f["az_alt_after_goto"] = [round(last["az"], 3), round(last["alt"], 3)]
    f["j2000_error_arcsec_ra_dec"] = [
        round((last["ra_j2000"] - ra) * 15 * 3600 * math.cos(math.radians(dec)), 1),
        round((last["dec_j2000"] - dec) * 3600, 1),
    ]
    t0 = time.perf_counter()
    ctx.pwi.mount_tracking_off()
    fl = ctx.poll(3, t0=t0, interval=0.02, until=lambda s: not s["tracking"])
    f["tracking_flag_clear_after_tracking_off_s"] = fl[-1]["t"] if not fl[-1]["tracking"] else None
    a = ctx.poll(5, interval=0.5)
    f["off_axis_drift_5s_deg"] = [
        round(a[-1]["p0"] - a[0]["p0"], 4),
        round(a[-1]["p1"] - a[0]["p1"], 4),
    ]
    f["off_ra_change_arcsec_5s"] = round((a[-1]["ra_j2000"] - a[0]["ra_j2000"]) * 15 * 3600, 1)
    t0 = time.perf_counter()
    ctx.pwi.mount_tracking_on()
    fl = ctx.poll(3, t0=t0, interval=0.02, until=lambda s: s["tracking"])
    f["tracking_flag_set_after_tracking_on_s"] = fl[-1]["t"] if fl[-1]["tracking"] else None
    b = ctx.poll(8, interval=0.5)
    f["tracking_flag_after_tracking_on"] = b[-1]["tracking"]
    f["on_axis_drift_8s_deg"] = [
        round(b[-1]["p0"] - b[0]["p0"], 4),
        round(b[-1]["p1"] - b[0]["p1"], 4),
    ]
    f["on_ra_change_arcsec_8s"] = round((b[-1]["ra_j2000"] - b[0]["ra_j2000"]) * 15 * 3600, 2)
    f["on_dec_change_arcsec_8s"] = round((b[-1]["dec_j2000"] - b[0]["dec_j2000"]) * 3600, 2)
    f["on_measured_v0_degs"] = round(b[-1]["v0"], 5)
    ctx.pwi.mount_tracking_off()
    c = ctx.poll(1.0, interval=0.25)
    f["tracking_flag_after_tracking_off"] = c[-1]["tracking"]
    return f, {"goto": smp, "off": a, "on": b}


# ---------------------------------------------------------------- 5 ---
def make_geo_tle(lon_deg: float = 100.6, name: str = "SYNTH GEO") -> tuple[str, str, str]:
    """Synthetic near-geostationary TLE, epoch now, sub-satellite longitude ~lon_deg E."""
    now = datetime.now(UTC)
    t = _TS.from_datetime(now)
    gmst_deg = (t.gmst * 15.0) % 360.0
    mean_anom = (lon_deg + gmst_deg) % 360.0  # raan = argp = 0, inclination ~0
    sat = Satrec()
    sat.sgp4init(
        WGS72,
        "i",
        99999,
        _jd(now) - 2433281.5,  # days since 1949-12-31 00:00 UT
        0.0,
        0.0,
        0.0,
        0.0001,
        0.0,
        math.radians(0.05),
        math.radians(mean_anom),
        1.0027379 * 2 * math.pi / 1440.0,
        0.0,
    )
    l1, l2 = export_tle(sat)
    return name, l1, l2


def expected_altaz(l1: str, l2: str, when: list[datetime], lag_s: float = 0.0):
    """Geometric az/el of the TLE satellite from PWI4's site at the given UTC datetimes."""
    sat = EarthSatellite(l1, l2, "SYNTH", _TS)
    topos = wgs84.latlon(SITE_LAT, SITE_LON, elevation_m=SITE_ELEV_M)
    base = when[0]
    secs = np.array([(w - base).total_seconds() + lag_s for w in when])
    t = _TS.utc(
        base.year,
        base.month,
        base.day,
        base.hour,
        base.minute,
        base.second + base.microsecond / 1e6 + secs,
    )
    alt, az, _ = (sat - topos).at(t).altaz()
    return az.degrees, alt.degrees


def make_pass_tle(
    t_pass_s: float = 45.0, incl_deg: float = 53.0, alt_km: float = 800.0, name: str = "SYNTH LEO"
) -> tuple[str, str, str, dict[str, float]]:
    """Synthetic circular LEO passing ~65 deg elevation over the site t_pass_s from now."""
    now = datetime.now(UTC)
    mu, re_km = 398600.8, 6378.135
    a = re_km + alt_km
    n_rad_s = math.sqrt(mu / a**3)
    inc = math.radians(incl_deg)
    u = math.asin(math.sin(math.radians(SITE_LAT)) / math.sin(inc))  # ascending, over site lat
    t_pass = _TS.from_datetime(now + timedelta(seconds=t_pass_s))
    gmst = math.radians((t_pass.gmst * 15.0) % 360.0)
    best = None
    for shift in np.arange(-8.0, 8.01, 0.25):
        raan = (
            math.radians(SITE_LON + shift)
            + gmst
            - math.atan2(math.cos(inc) * math.sin(u), math.cos(u))
        ) % (2 * math.pi)
        m0 = (u - n_rad_s * t_pass_s) % (2 * math.pi)
        sat = Satrec()
        sat.sgp4init(
            WGS72, "i", 99998, _jd(now) - 2433281.5, 0.0, 0.0, 0.0, 0.0001, 0.0,
            inc, m0, n_rad_s * 60.0, raan,
        )  # fmt: skip
        l1, l2 = export_tle(sat)
        es = EarthSatellite(l1, l2, name, _TS)
        topos = wgs84.latlon(SITE_LAT, SITE_LON, elevation_m=SITE_ELEV_M)
        tt = _TS.from_datetimes([now + timedelta(seconds=float(x)) for x in range(0, 121, 5)])
        alt, _, _ = (es - topos).at(tt).altaz()
        mx = float(alt.degrees.max())
        if mx <= 80 and (best is None or abs(mx - 65) < abs(best[0] - 65)):
            best = (mx, shift, l1, l2)
    info = {"max_el_deg": round(best[0], 1), "lon_shift_deg": float(best[1])}
    return name, best[2], best[3], info


def _follow_and_compare(ctx: Ctx, name, l1, l2, dur: float, interval: float = 0.5):
    f: dict[str, Any] = {}
    t0 = time.perf_counter()
    ctx.pwi.mount_follow_tle(name, l1, l2)  # PWI4 wants the name line first
    smp = ctx.poll(dur, t0=t0, interval=interval)
    f["tracking_flag_seen_true"] = any(s["tracking"] for s in smp)
    f["tracking_flag_end"] = smp[-1]["tracking"]
    when = [parse_utc(s["resp_utc"]) for s in smp]
    eaz, ealt = expected_altaz(l1, l2, when)
    err = np.array(
        [sep_deg(s["az"], s["alt"], a, b) for s, a, b in zip(smp, eaz, ealt, strict=True)]
    )
    f["err_first_sample_deg"] = round(float(err[0]), 3)
    acq = next((i for i, e in enumerate(err) if e < 0.1), None)
    f["acquired_within_0p1deg_s"] = round(smp[acq]["t"], 1) if acq is not None else None
    if acq is None or len(smp) - acq < 5:
        ctx.note(f"follow_tle {name}: never acquired within 0.1 deg; no accuracy comparison")
        return f, smp
    use = smp[acq + 2 :]
    w = when[acq + 2 :]
    az = [s["az"] for s in use]
    alt = [s["alt"] for s in use]
    f["n_compared"] = len(use)
    f["clock_resp_minus_pc_s"] = round((when[-1] - datetime.now(UTC)).total_seconds(), 2)
    mnt = [parse_utc(s["mount_utc"]) or r for s, r in zip(use, w, strict=True)]
    f["clock_mount_minus_resp_s"] = round(
        float(np.mean([(a - b).total_seconds() for a, b in zip(mnt, w, strict=True)])), 3
    )
    eaz, ealt = expected_altaz(l1, l2, w)
    e = np.array([sep_deg(a, b, c, d) for a, b, c, d in zip(az, alt, eaz, ealt, strict=True)])
    f["err_vs_sgp4_deg"] = {
        "mean": round(float(e.mean()), 5),
        "p95": round(float(np.percentile(e, 95)), 5),
        "max": round(float(e.max()), 5),
        "az_bias": round(float(np.mean([wrap180(a - c) for a, c in zip(az, eaz, strict=True)])), 5),
        "alt_bias": round(float(np.mean([b - d for b, d in zip(alt, ealt, strict=True)])), 5),
    }
    best = None
    for lag in np.arange(-4.0, 4.01, 0.125):
        la, lb = expected_altaz(l1, l2, w, float(lag))
        rms = math.sqrt(
            float(
                np.mean(
                    [sep_deg(a, b, c, d) ** 2 for a, b, c, d in zip(az, alt, la, lb, strict=True)]
                )
            )
        )
        if best is None or rms < best[1]:
            best = (float(lag), rms)
    f["best_time_lag_s"] = best[0]
    f["rms_deg_at_best_lag"] = round(best[1], 5)
    f["az_alt_end"] = [round(use[-1]["az"], 3), round(use[-1]["alt"], 3)]
    return f, smp


def s05_follow_tle(ctx: Ctx):
    f: dict[str, Any] = {}
    name, l1, l2 = make_geo_tle()
    sat = EarthSatellite(l1, l2, name, _TS)
    sp = wgs84.subpoint(sat.at(_TS.now()))
    geo, smp_geo = _follow_and_compare(ctx, name, l1, l2, 35)
    geo["tle"] = [name, l1, l2]
    geo["our_subsatellite_lon_lat_deg"] = [
        round(float(sp.longitude.degrees), 3),
        round(float(sp.latitude.degrees), 3),
    ]
    f["geo"] = geo
    ctx.pwi.mount_stop()
    ctx.pwi.mount_tracking_off()
    name, l1, l2, info = make_pass_tle()
    leo, smp_leo = _follow_and_compare(ctx, name, l1, l2, 100, interval=0.25)
    leo["tle"] = [name, l1, l2]
    leo["pass"] = info
    f["leo_pass"] = leo
    return f, {"geo": smp_geo, "leo": smp_leo}


# ---------------------------------------------------------------- 6 ---
def _run_path(ctx: Ctx, update_wrap, az0: float = 150.0, alt0: float = 50.0, dur: float = 40.0):
    """Upload a 1 Hz alt/az path starting 4 s from now and follow it; return findings, samples."""
    r: dict[str, Any] = {}
    ctx.goto_altaz_wait(alt0, az0)
    r["wrap_min_before"] = ctx.status().mount.axis0_wrap_range_min_degs
    start = datetime.now(UTC) + timedelta(seconds=4)

    def path(sec: float) -> tuple[float, float]:
        return az0 + 0.25 * sec, alt0 + 4.0 * math.sin(2 * math.pi * sec / 40.0)

    pts = []
    for i in range(int(dur) + 1):
        az, alt = path(i)
        pts.append((_jd(start + timedelta(seconds=i)), f"{az:.6f}", f"{alt:.6f}"))
    ctx.pwi.mount_custom_path_new("altaz")
    ctx.pwi.mount_custom_path_add_point_list(pts)
    st = ctx.pwi.mount_custom_path_apply(update_wrap=update_wrap)
    r["after_apply"] = {"tracking": st.mount.is_tracking, "slewing": st.mount.is_slewing}
    smp = ctx.poll(dur + 8, interval=0.5)
    r["wrap_min_after"] = smp and ctx.status().mount.axis0_wrap_range_min_degs
    r["tracking_seen_true"] = any(s["tracking"] for s in smp)
    r["max_abs_az_excursion_from_start_deg"] = round(max(abs(s["az"] - az0) for s in smp), 1)

    def rel(s):
        return (parse_utc(s["resp_utc"]) - start).total_seconds()

    on = [s for s in smp if 0 < rel(s) < dur and not s["slewing"]]
    r["first_on_path_s"] = round(rel(on[0]), 1) if on else None
    use = [s for s in on if s["t"] > 0] if on else []
    r["n_compared"] = len(use)
    if use:

        def errs(lag: float):
            return [sep_deg(s["az"], s["alt"], *path(rel(s) + lag)) for s in use]

        e0 = errs(0.0)
        r["err_deg_at_resp_ts"] = {
            "mean": round(float(np.mean(e0)), 5),
            "max": round(float(np.max(e0)), 5),
        }
        lag_best = min(np.arange(-3, 3.01, 0.25), key=lambda lag: float(np.mean(errs(float(lag)))))
        r["best_time_lag_s"] = float(lag_best)
        r["mean_err_deg_at_best_lag"] = round(float(np.mean(errs(float(lag_best)))), 5)
    r["final_az_alt"] = [round(smp[-1]["az"], 3), round(smp[-1]["alt"], 3)]
    return r, smp


def s06_custom_path(ctx: Ctx):
    f: dict[str, Any] = {}
    accepted, rejected = [], {}
    for ct in ("altaz", "raw", "radec", "radec_j2000", "j2000", "azel", "apparent", "icrs"):
        try:
            ctx.pwi.mount_custom_path_new(ct)
            accepted.append(ct)
        except Exception as exc:  # noqa: BLE001
            rejected[ct] = str(exc)
    f["coord_types_accepted"] = accepted
    f["coord_types_rejected"] = rejected
    try:
        ctx.pwi.mount_goto_coord_pair(0, 0, "bogus")
    except Exception as exc:  # noqa: BLE001
        f["goto_coord_pair_bogus_error"] = str(exc)
    ctx.pwi.mount_custom_path_new("altaz")
    try:
        ctx.pwi.mount_custom_path_add_point_list([(_jd(datetime.now(UTC)), 150.0, 50.0)])
        ctx.pwi.mount_custom_path_apply()
    except Exception as exc:  # noqa: BLE001
        f["apply_with_1_point_error"] = str(exc)
    f["point_format"] = "POST /mount/custom_path/add_point_list, form field data = lines 'jd,c0,c1'"
    f["coord_order_altaz"] = "c0 = azimuth deg, c1 = altitude deg; jd = UTC Julian date; >2 points"
    raw = {}
    for label, uw in (("update_wrap_default", None), ("update_wrap_0", False)):
        r, smp = _run_path(ctx, uw)
        f[label] = r
        raw[label] = smp
        ctx.pwi.mount_stop()
        ctx.pwi.mount_tracking_off()
    return f, raw


# ---------------------------------------------------------------- 7 ---
def _rate_zero(s) -> bool:
    return abs(s["v0"] or 0) < 0.01 and abs(s["off0_rate"] or 0) < 1e-6


def s07_offsets(ctx: Ctx):
    f: dict[str, Any] = {}
    ctx.pwi.mount_tracking_off()
    ctx.goto_altaz_wait(30.0, 90.0)
    base = ctx.sample()
    rate_arcsec = 1800.0  # 0.5 deg/s
    ctx.pwi.mount_offset(axis0_set_rate_arcsec_per_sec=rate_arcsec)
    run = ctx.poll(3, interval=0.1)
    f["idle_rate_run"] = {
        "requested_deg_s": rate_arcsec * ARCSEC,
        "measured_v0_degs_end": round(run[-1]["v0"], 4),
        "off0_rate_reported_arcsec_s": run[-1]["off0_rate"],
        "axis0_delta_deg_3s": round(run[-1]["p0"] - base["p0"], 3),
    }
    t0 = time.perf_counter()
    ctx.pwi.mount_offset(axis0_stop_rate=0)
    post = ctx.poll(10, t0=t0, until=lambda s: s["t"] > 0.1 and _rate_zero(s))
    f["stop_rate_latency_s"] = post[-1]["t"] if _rate_zero(post[-1]) else None
    f["stop_rate_off_total_arcsec"] = post[-1]["off0_total"]
    p_before_reset = post[-1]["p0"]

    def reset_done(s):
        return s["t"] > 0.2 and abs(s["off0_total"] or 0) < 0.5 and settled(s, 0.01)

    t0 = time.perf_counter()
    ctx.pwi.mount_offset(axis0_reset=0)
    rs = ctx.poll(15, t0=t0, until=reset_done)
    f["reset_return_latency_s"] = rs[-1]["t"]
    f["reset_returned_deg"] = round(p_before_reset - rs[-1]["p0"], 3)
    # start rate then reset directly (jog release)
    ctx.pwi.mount_offset(axis0_set_rate_arcsec_per_sec=rate_arcsec)
    time.sleep(1.5)
    t0 = time.perf_counter()
    ctx.pwi.mount_offset(axis0_reset=0)
    jr = ctx.poll(15, t0=t0, until=reset_done)
    f["rate_then_reset_settle_s"] = jr[-1]["t"]
    f["rate_then_reset_rate_zero_s"] = _first(jr, _rate_zero)
    # step
    base = ctx.sample()
    t0 = time.perf_counter()
    ctx.pwi.mount_offset(axis0_add_arcsec=120)
    stp = ctx.poll(
        15,
        t0=t0,
        until=lambda s: s["t"] > 0.2 and abs((s["off0_total"] or 0) - 120) < 1 and settled(s, 0.01),
    )
    f["step_120as_complete_s"] = stp[-1]["t"]
    f["step_axis0_delta_arcsec"] = round((stp[-1]["p0"] - base["p0"]) * 3600, 2)
    f["step_off0_total_arcsec"] = stp[-1]["off0_total"]
    ctx.pwi.mount_offset(axis0_reset=0)
    ctx.poll(8, until=lambda s: abs(s["off0_total"] or 0) < 0.5 and settled(s, 0.01))
    # while tracking
    ctx.pwi.mount_goto_ra_dec_j2000(ctx.status().site.lmst_hours % 24, 30.0)
    ctx.poll(60, until=lambda s: s["t"] > 0.5 and settled(s))
    ctx.pwi.mount_tracking_on()
    tb = ctx.poll(4, interval=0.25)
    v_tr = float(np.mean([s["v0"] for s in tb]))
    ctx.pwi.mount_offset(axis0_set_rate_arcsec_per_sec=600.0)
    tr = ctx.poll(3, interval=0.25)
    v_off = float(np.mean([s["v0"] for s in tr[4:]]))
    f["tracking_nudge"] = {
        "tracking_flag": tr[-1]["tracking"],
        "v0_tracking_only_degs": round(v_tr, 5),
        "v0_with_600as_rate_degs": round(v_off, 5),
        "delta_degs": round(v_off - v_tr, 5),
        "expected_degs": round(600 * ARCSEC, 5),
    }
    ctx.pwi.mount_offset(axis0_stop_rate=0)
    ra0 = ctx.sample()
    ctx.pwi.mount_offset(ra_add_arcsec=60)
    ra1 = ctx.poll(6, interval=0.25)
    f["tracking_ra_step_60as"] = {
        "off_ra_total": ra1[-1]["off_ra_total"],
        "ra_change_arcsec": round((ra1[-1]["ra_j2000"] - ra0["ra_j2000"]) * 15 * 3600, 1),
        "tracking_after": ra1[-1]["tracking"],
    }
    return f, {"idle_run": run, "stop_rate": post, "reset": rs, "step": stp, "tracking": tr}


# ---------------------------------------------------------------- 8 ---
def s08_watchdog(ctx: Ctx):
    f: dict[str, Any] = {}
    # offsets only move the (simulated) mount while it is tracking a target, so track first
    ctx.pwi.mount_goto_ra_dec_j2000(ctx.status().site.lmst_hours % 24, 30.0)
    ctx.poll(60, until=lambda s: s["t"] > 0.5 and settled(s))
    ctx.pwi.mount_tracking_on()
    time.sleep(1.0)
    p_start = ctx.sample()["p0"]
    ctx.pwi.mount_offset(axis0_set_rate_arcsec_per_sec=1800.0)  # 0.5 deg/s
    time.sleep(10.0)  # fully silent: no requests at all
    after = ctx.sample()
    f["silent_s"] = 10
    f["tracking_flag_after_silence"] = after["tracking"]
    f["axis0_moved_deg"] = round(after["p0"] - p_start, 3)
    f["expected_if_no_watchdog_deg"] = 5.0 - 10 * 0.0133  # 0.5 deg/s minus sidereal drift
    f["off0_rate_after_silence_arcsec_s"] = after["off0_rate"]
    f["measured_v0_after_silence_degs"] = round(after["v0"], 4)
    f["pwi4_stopped_by_itself"] = abs(after["off0_rate"] or 0) < 1e-6
    f["conclusion"] = (
        "PWI4 stopped the rate on its own"
        if f["pwi4_stopped_by_itself"]
        else "NO watchdog: rate offset kept running with no client traffic"
    )
    t0 = time.perf_counter()
    ctx.pwi.mount_offset(axis0_stop_rate=0)
    post = ctx.poll(10, t0=t0, until=lambda s: s["t"] > 0.1 and _rate_zero(s))
    f["explicit_stop_latency_s"] = post[-1]["t"]
    ctx.pwi.mount_offset(axis0_reset=0)
    ctx.poll(15, until=lambda s: abs(s["off0_total"] or 0) < 0.5 and settled(s, 0.05))
    ctx.pwi.mount_tracking_off()
    return f, {"after": after, "post": post}


# ---------------------------------------------------------------- 9 ---
def s09_status_latency(ctx: Ctx):
    f: dict[str, Any] = {}
    smp = ctx.poll(30.0, interval=0.1)
    lat = [s["lat_ms"] for s in smp]
    st = stats(lat)
    f["n"] = st["n"]
    f["latency_ms"] = {k: round(v, 2) for k, v in st.items() if k != "n"}
    ts = [s["t"] for s in smp]
    f["achieved_rate_hz"] = round((len(ts) - 1) / (ts[-1] - ts[0]), 2)
    f["requests_over_100ms"] = sum(1 for x in lat if x > 100)
    return f, {"lat_ms": lat}


# --------------------------------------------------------------- 10 ---
def s10_disconnect(ctx: Ctx):
    f: dict[str, Any] = {}
    ctx.safe_state()
    ctx.pwi.mount_disable(0)
    ctx.pwi.mount_disable(1)
    time.sleep(0.5)
    st = ctx.pwi.status()
    f["axes_enabled_before_disconnect"] = [st.mount.axis0.is_enabled, st.mount.axis1.is_enabled]
    ctx.pwi.mount_disconnect()
    time.sleep(0.5)
    st = ctx.pwi.status()
    f["is_connected_final"] = st.mount.is_connected
    f["axes_enabled_final"] = [st.mount.axis0.is_enabled, st.mount.axis1.is_enabled]
    return f, {}


SCENARIOS = [
    ("01_connect_enable_home", s01_connect_home),
    ("02_goto_altaz", s02_goto_altaz),
    ("03_stop_midslew", s03_stop_midslew),
    ("04_radec_tracking", s04_radec_tracking),
    ("05_follow_tle", s05_follow_tle),
    ("06_custom_path", s06_custom_path),
    ("07_offsets_jog_step", s07_offsets),
    ("08_watchdog", s08_watchdog),
    ("09_status_latency", s09_status_latency),
    ("10_disconnect", s10_disconnect),
]
