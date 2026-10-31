# ruff: noqa: F811
"""T3.3 gate measurements against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

Covers: H4 vs PWI4's own alt/az, TRACK (sidereal accuracy, parity with PWI4's Track after each kind
of stopped goto), STOP from TRACKING, TRACK refusals (Lim EL, Sun disc) and the not-homed guard.

Throwaway gateway subprocesses on the shared test port (see test_control_pwi4sim); the Sun is
pinned with the test-only PW_TEST_SUN_ALTAZ so no case depends on the real Sun or the time of day.
`sim_guard.assert_simulator()` runs in the shared `pwi` fixture before any PWI4 command. PWI4.cfg
is only READ. Direct PWI4 calls here (follow_tle, a below-Lim-EL start pose) go through the test's
own pwi4_client after that guard, never through the gateway.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from gateway.ephem import Ephem
from gateway.snapshot import jd_from_utc, lmst_hours_from
from gateway.tests.test_control_pwi4sim import (  # noqa: F401  (pwi is a fixture)
    RESULTS,
    ROOT,
    Client,
    Sampler,
    pwi,
    run,
)
from gateway.tests.test_sequences_pwi4sim import (
    SeqClient,
    connect_sim,
    disconnect_sim,
    start_gateway,
)
from gateway.transforms import refraction_arcsec

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

SUN_FAR = "-60,0"  # pinned Sun far below the horizon: the Sun checks are moot
HOME_ALT, HOME_AZ = 85.0, 180.0
_EPHEM: Ephem | None = None


# ------------------------------------------------------------------ helpers


def ephem() -> Ephem:
    global _EPHEM
    if _EPHEM is None:
        _EPHEM = Ephem(ROOT / "data" / "ephemeris" / "de421.bsp")
        assert _EPHEM.available, _EPHEM.error
    return _EPHEM


def sep_arcsec(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle separation (arcsec) of two directions given as (lon, lat) in degrees."""
    a1, d1, a2, d2 = map(math.radians, (lon1, lat1, lon2, lat2))
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((a2 - a1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h)))) * 3600.0


def radec_sep_arcsec(ra1_h: float, dec1: float, ra2_h: float, dec2: float) -> float:
    return sep_arcsec(ra1_h * 15.0, dec1, ra2_h * 15.0, dec2)


def status_time(s) -> datetime:
    return datetime.fromisoformat(s.response.timestamp_utc).replace(tzinfo=UTC)


def site_of(s) -> tuple[float, float, float]:
    return (s.site.latitude_degs, s.site.longitude_degs, s.site.height_meters)


def h4_altaz(s) -> tuple[float, float]:
    """H4 of PWI4's own reported J2000 RA/Dec at PWI4's status time and site."""
    m = s.mount
    return ephem().radec_j2000_altaz(site_of(s), m.ra_j2000_hours, m.dec_j2000_degs, status_time(s))


async def status(pwi):
    return await asyncio.to_thread(pwi.status)


async def wait_stationary(pwi, timeout: float = 30.0) -> None:
    """Test infrastructure: wait until the sim mount stands still and is not slewing."""
    t0 = time.monotonic()
    last = None
    still = 0
    while time.monotonic() - t0 < timeout:
        m = (await status(pwi)).mount
        pos = (m.axis0.position_degs, m.axis1.position_degs)
        if last is not None and max(abs(a - b) for a, b in zip(pos, last, strict=True)) < 0.002:
            still += 1
            if still >= 4 and not m.is_slewing:
                return
        else:
            still = 0
        last = pos
        await asyncio.sleep(0.25)
    raise AssertionError("mount did not settle")


async def wait_arrived(pwi, timeout: float = 90.0) -> None:
    """Test infrastructure: wait until a goto has really finished: not slewing and both axes
    within 5 arcsec of their targets, 3 reads in a row (a slew that has not started yet, or has
    not been picked up yet, is not 'stationary')."""
    t0 = time.monotonic()
    ok = 0
    while time.monotonic() - t0 < timeout:
        m = (await status(pwi)).mount
        d0, d1 = m.axis0.dist_to_target_arcsec, m.axis1.dist_to_target_arcsec
        if not m.is_slewing and abs(d0) < 5.0 and abs(d1) < 5.0:
            ok += 1
            if ok >= 3:
                return
        else:
            ok = 0
        await asyncio.sleep(0.3)
    raise AssertionError("mount did not arrive")


async def track_and_land(c: Client, pwi, timeout: float = 60.0) -> dict:
    """TRACK (a bare tracking_on, T3.3b / P19) then wait for any PWI4 turn to finish. PWI4 may
    normalise axis 0 into its wrap window by turning (up to ~360 deg); that is intended. Returns
    the turn size / time and how far the final RA/Dec is from the RA/Dec of the TRACK moment."""
    s_pre = await status(pwi)
    m_pre = s_pre.mount
    ax_pre = m_pre.axis0.position_degs
    ack = await c.cmd("tracking", {"on": True})
    assert ack["ok"], ack
    t0 = time.monotonic()
    calm = 0
    slewing_s = 0.0
    last = t0
    ax_max_dev = 0.0
    while time.monotonic() - t0 < timeout:
        s = await status(pwi)
        m = s.mount
        now = time.monotonic()
        if m.is_slewing:
            slewing_s += now - last
        last = now
        ax_max_dev = max(ax_max_dev, abs(m.axis0.position_degs - ax_pre))
        if c.state == "TRACKING" and not m.is_slewing:
            calm += 1
            if calm >= 4:
                break
        else:
            calm = 0
        await asyncio.sleep(0.5)
    assert c.state == "TRACKING", c.state
    s_end = await status(pwi)
    m_end = s_end.mount
    err = radec_sep_arcsec(
        m_pre.ra_j2000_hours, m_pre.dec_j2000_degs, m_end.ra_j2000_hours, m_end.dec_j2000_degs
    )
    return {
        "turn_deg": round(ax_max_dev, 2),
        "axis0_pre_end": (round(ax_pre, 2), round(m_end.axis0.position_degs, 2)),
        "slewing_s": round(slewing_s, 1),
        "total_s": round(time.monotonic() - t0, 1),
        "final_radec_err_arcsec": round(err, 2),
    }


def assert_no_false_stops(g) -> None:
    """The safety monitor must not stop a PWI4 turn that is clear of the Sun and above Lim EL."""
    bad = [e for e in g.audit_events() if e["event"] in ("limit_stop", "stall_stop")]
    assert not bad, bad


SUN_CLEAR_DEG = 30.0  # PWI4 itself cuts slews short near the (real) Sun: stay well clear


def path_clear_of_sun(a0: float, z0: float, a1: float, z1: float, sun, n: int = 60) -> bool:
    """True when the straight alt/az path (azimuth the short way) stays SUN_CLEAR_DEG from `sun`."""
    dz = (z1 - z0 + 180.0) % 360.0 - 180.0
    for i in range(n + 1):
        u = i / n
        if sep_arcsec(z0 + dz * u, a0 + (a1 - a0) * u, sun[1], sun[0]) < SUN_CLEAR_DEG * 3600.0:
            return False
    return True


async def _goto_one(c: Client, pwi, alt: float, az: float) -> None:
    ack = await c.cmd("goto_altaz", {"alt": alt, "az": az})
    assert ack["ok"], ack
    await asyncio.sleep(0.5)
    await wait_stationary(pwi)
    await c.wait_state("STOPPED", 10)


async def goto_altaz_and_wait(c: Client, pwi, alt: float, az: float) -> None:
    """Gateway Az/El goto and wait. The real Sun must not matter at any time of day (the gateway
    runs with a pinned Sun, but PWI4 stops slews that run into the real one): when the straight
    path would pass near the real Sun it is routed through a waypoint altitude (20 or 72 deg,
    whichever is farther from the Sun's altitude; tracking is not possible near the zenith):
    vertical leg, azimuth sweep at that altitude, vertical leg. A target azimuth within 40 deg of
    the real Sun is shifted by 90/-90/180 deg (the tests only need some pose at that altitude)."""
    s = await status(pwi)
    m = s.mount
    sun = ephem().sun_altaz(site_of(s))
    for shift in (0.0, 90.0, -90.0, 180.0):  # a TARGET near the real Sun is moved aside
        if sep_arcsec(az + shift, alt, sun[1], sun[0]) >= 40.0 * 3600.0:
            az = (az + shift) % 360.0
            break
    if not path_clear_of_sun(m.altitude_degs, m.azimuth_degs, alt, az, sun):
        wa = 20.0 if abs(20.0 - sun[0]) >= abs(72.0 - sun[0]) else 72.0
        await _goto_one(c, pwi, wa, m.azimuth_degs)
        await _goto_one(c, pwi, wa, az)
    await _goto_one(c, pwi, alt, az)


async def make_stopped(c: Client, pwi) -> None:
    """Gateway STOP, direct tracking off (infrastructure), wait for a stationary STOPPED mount."""
    await c.cmd("stop")
    await asyncio.to_thread(pwi.mount_tracking_off)
    await wait_stationary(pwi)
    await c.wait_state("STOPPED", 10)


def other_az(az: float, delta: float) -> float:
    return (az + delta) % 360.0


def geo_tle(lon_east: float, when: datetime) -> tuple[str, str, str]:
    """A synthetic geostationary TLE (epoch = `when`) whose sub-satellite point is on the
    meridian of `lon_east`, so it sits high in the sky of a site on that meridian."""
    yy = when.year % 100
    doy = (
        when.timetuple().tm_yday
        + (when.hour * 3600 + when.minute * 60 + when.second + when.microsecond / 1e6) / 86400.0
    )
    mean_anom = (lmst_hours_from(jd_from_utc(when), lon_east) * 15.0) % 360.0

    def cks(line: str) -> str:
        return str(sum(int(ch) if ch.isdigit() else (1 if ch == "-" else 0) for ch in line) % 10)

    l1 = f"1 99999U 24001A   {yy:02d}{doy:012.8f}  .00000000  00000-0  00000-0 0  999"
    # inclination 0.01 deg, RAAN 0, e ~ 0, arg. of perigee 0, mean motion 1.00273791 rev/day
    l2 = f"2 99999   0.0100   0.0000 0000001   0.0000 {mean_anom:8.4f}  1.00273791    1"
    return "T33-GEO", l1 + cks(l1), l2 + cks(l2)


async def sat_pose_after_follow(pwi, tle, seconds: float = 25.0) -> tuple[float, float]:
    """Let PWI4 follow the TLE until it has acquired, return (alt, az); then the caller stops."""
    await asyncio.to_thread(pwi.mount_follow_tle, *tle)
    await asyncio.sleep(seconds)
    m = (await status(pwi)).mount
    return m.altitude_degs, m.azimuth_degs


# ------------------------------------------------------------------ H4 vs PWI4


def test_h4_vs_pwi4_reported_altaz(pwi, tmp_path):
    """T3.3b (P18): H4's default frame is PWI4's (observed, refracted). PWI4's reported alt/az
    vs H4(PWI4's own J2000 RA/Dec) at the status timestamp, over alt 12..86 and several azimuths.

    Measured on the simulator: the refraction model (Saemundsson x 0.9865) matches to < 1 arcsec,
    but the simulator's RA/Dec carry a constant Earth-rotation offset of about -0.7 s of time
    (up to ~10 arcsec along the diurnal circle; PWI4's own UT1/clock handling, not refraction).
    So the test reports the raw residual (default time) and the residual after fitting that one
    constant offset: <= 2.5 arcsec. The raw residual is bounded by the offset's size (<= 15")."""
    g = start_gateway(tmp_path, SUN_FAR)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 8)
            await make_stopped(c, pwi)
            m0 = (await status(pwi)).mount
            az0 = m0.azimuth_degs
            snaps = []
            for alt, daz in [
                (55.0, 0.0),
                (40.0, 70.0),
                (75.0, 150.0),
                (20.0, 200.0),
                (86.0, 250.0),
            ]:
                await goto_altaz_and_wait(c, pwi, alt, other_az(az0, daz))
                await make_stopped(c, pwi)
                snaps.append(await status(pwi))

            def resid(s, dt: float):
                m = s.mount
                t = status_time(s) + timedelta(seconds=dt)
                h_alt, h_az = ephem().radec_j2000_altaz(
                    site_of(s), m.ra_j2000_hours, m.dec_j2000_degs, t
                )
                return sep_arcsec(m.azimuth_degs, m.altitude_degs, h_az, h_alt)

            raw = [resid(s, 0.0) for s in snaps]
            offsets = [x / 20.0 for x in range(-40, 41)]  # -2 .. +2 s in 0.1 s steps
            best_dt = min(offsets, key=lambda dt: max(resid(s, dt) for s in snaps))
            fitted = [resid(s, best_dt) for s in snaps]
            m_alt = [
                (
                    round(s.mount.altitude_degs, 3),
                    round(refraction_arcsec(s.mount.altitude_degs), 1),
                )
                for s in snaps
            ]
            RESULTS["h4_vs_pwi4"] = {
                "raw_arcsec": [round(x, 2) for x in raw],
                "fitted_time_offset_s": best_dt,
                "fitted_arcsec": [round(x, 2) for x in fitted],
                "alt_refraction_arcsec": m_alt,
            }
            print(
                f"\nH4 (refracted) vs PWI4: raw {[round(x, 1) for x in raw]} arcsec; "
                f"constant clock offset {best_dt:+.2f} s -> {[round(x, 2) for x in fitted]} arcsec"
            )
            assert max(fitted) <= 2.5, (best_dt, fitted)
            assert -1.5 <= best_dt <= 0.5, best_dt
            assert max(raw) <= 15.0, raw
            await c.close()

        run(go())
    finally:
        g.kill()


# ------------------------------------------------------------------ TRACK


def test_track_from_stopped_is_sidereal_and_stop_holds(pwi, tmp_path):
    g = start_gateway(tmp_path, SUN_FAR)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 8)
            await make_stopped(c, pwi)
            m = (await status(pwi)).mount
            await goto_altaz_and_wait(c, pwi, 55.0, other_az(m.azimuth_degs, 60.0))
            m = (await status(pwi)).mount
            assert not m.is_tracking

            ack = await c.cmd("tracking", {"on": True})
            assert ack["ok"], ack
            await c.wait_state("TRACKING", 5)
            await asyncio.sleep(2.0)  # settle
            s0 = await status(pwi)
            m0 = s0.mount
            assert m0.is_tracking
            h0_alt, h0_az = h4_altaz(s0)
            off0 = (m0.altitude_degs - h0_alt, m0.azimuth_degs - h0_az)
            samples = []
            t_start = time.monotonic()
            while time.monotonic() - t_start < 60.0:
                await asyncio.sleep(10.0)
                s = await status(pwi)
                m = s.mount
                samples.append(
                    (
                        round(time.monotonic() - t_start, 1),
                        round(radec_sep_arcsec(m0.ra_j2000_hours, m0.dec_j2000_degs,
                                               m.ra_j2000_hours, m.dec_j2000_degs), 3),
                    )
                )  # fmt: skip
            s1 = await status(pwi)
            m1 = s1.mount
            drift = radec_sep_arcsec(
                m0.ra_j2000_hours, m0.dec_j2000_degs, m1.ra_j2000_hours, m1.dec_j2000_degs
            )
            # alt/az must follow H4 of the START RA/Dec: use the start RA/Dec at the end time
            end_alt, end_az = ephem().radec_j2000_altaz(
                site_of(s1), m0.ra_j2000_hours, m0.dec_j2000_degs, status_time(s1)
            )
            moved = sep_arcsec(m0.azimuth_degs, m0.altitude_degs, m1.azimuth_degs, m1.altitude_degs)
            follow_err = sep_arcsec(m1.azimuth_degs, m1.altitude_degs, end_az, end_alt)
            off_change = (
                math.hypot(
                    (m1.altitude_degs - end_alt) - off0[0],
                    ((m1.azimuth_degs - end_az) - off0[1])
                    * math.cos(math.radians(m1.altitude_degs)),
                )
                * 3600.0
            )
            RESULTS["track_60s"] = {
                "radec_j2000_drift_arcsec_series": samples,
                "radec_j2000_drift_arcsec": round(drift, 3),
                "altaz_moved_arcsec": round(moved, 1),
                "altaz_vs_h4_of_start_radec_arcsec": round(follow_err, 2),
                "altaz_vs_h4_offset_change_arcsec": round(off_change, 2),
            }
            print("\nTRACK 60 s:", RESULTS["track_60s"])
            assert drift <= 1.0
            assert moved > 600.0  # ~15"/s * 60 s: it really follows the sky
            assert off_change <= 3.0

            # ---- STOP from TRACKING: is_tracking false within 1 s, az/el stays put
            smp = Sampler(pwi).start()
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            ack = await c.cmd("stop")
            assert ack["ok"], ack
            await asyncio.sleep(4.6)
            smp.close()
            after = [x for x in smp.samples if x[0] >= t0]
            t_false = next((x[0] for x in after if not x[4]), None)
            assert t_false is not None, "is_tracking never went false"
            lat = t_false - t0
            win = [x for x in after if t0 + 1.0 <= x[0] <= t0 + 4.0]
            assert len(win) > 20
            drift_deg = max(max(abs(x[1] - win[0][1]), abs(x[2] - win[0][2])) for x in win)
            RESULTS["stop_from_tracking"] = {
                "is_tracking_false_after_s": round(lat, 3),
                "axis_drift_deg_over_3s": round(drift_deg, 5),
            }
            print("\nSTOP from TRACKING:", RESULTS["stop_from_tracking"])
            assert lat <= 1.0, lat
            assert drift_deg <= 0.01, drift_deg
            await c.wait_state("STOPPED", 5)
            await c.close()

        run(go())
    finally:
        g.kill()
    ev = [e for e in g.audit_events() if e["event"] == "track_start"]
    assert len(ev) == 1 and ev[0]["ra_h"] is not None and ev[0]["alt"] is not None


@pytest.mark.parametrize("kind", ["goto_radec_j2000", "goto_altaz", "follow_tle"])
def test_track_after_stopped_goto_holds_current_radec(pwi, tmp_path, kind):
    """Q1 parity: after a goto / follow stopped mid-slew, TRACK holds the RA/Dec of the stop
    moment; it does not return to the old target."""
    g = start_gateway(tmp_path, SUN_FAR)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 8)
            await make_stopped(c, pwi)
            s0 = await status(pwi)
            start_alt, start_az = 50.0, other_az(s0.mount.azimuth_degs, 0.0)
            await goto_altaz_and_wait(c, pwi, start_alt, start_az)
            s0 = await status(pwi)
            start_az = s0.mount.azimuth_degs
            site = site_of(s0)
            t_goal = other_az(start_az, 80.0)
            goal_alt = 58.0

            if kind == "follow_tle":
                tle = geo_tle(site[1], datetime.now(UTC))
                target_alt, target_az = await sat_pose_after_follow(pwi, tle)
                await make_stopped(c, pwi)
                await goto_altaz_and_wait(c, pwi, start_alt, start_az)  # back to the start
                await asyncio.to_thread(pwi.mount_follow_tle, *tle)
            elif kind == "goto_radec_j2000":
                target_alt, target_az = goal_alt, t_goal
                ra_h, dec = ephem().altaz_radec_j2000(site, goal_alt, t_goal, datetime.now(UTC))
                ack = await c.cmd("goto_radec_j2000", {"ra_h": ra_h, "dec_deg": dec})
                assert ack["ok"], ack
            else:
                target_alt, target_az = goal_alt, t_goal
                ack = await c.cmd("goto_altaz", {"alt": goal_alt, "az": t_goal})
                assert ack["ok"], ack

            await asyncio.sleep(1.5)
            mid = (await status(pwi)).mount
            ack = await c.cmd("stop")
            assert ack["ok"], ack
            await c.wait_state("STOPPED", 8)
            await wait_stationary(pwi)
            s_stop = await status(pwi)
            ms = s_stop.mount
            assert not ms.is_tracking
            sep_stop_target = sep_arcsec(ms.azimuth_degs, ms.altitude_degs, target_az, target_alt)
            sep_start_stop = sep_arcsec(ms.azimuth_degs, ms.altitude_degs, start_az, start_alt)

            res = await track_and_land(c, pwi)
            res.update(
                {
                    "slewing_at_stop": bool(mid.is_slewing),
                    "stop_to_target_deg": round(sep_stop_target / 3600.0, 2),
                    "stop_to_start_deg": round(sep_start_stop / 3600.0, 2),
                }
            )
            m2 = (await status(pwi)).mount
            to_target = sep_arcsec(m2.azimuth_degs, m2.altitude_degs, target_az, target_alt)
            res["final_to_old_target_deg"] = round(to_target / 3600.0, 2)
            RESULTS[f"parity_{kind}"] = res
            print(f"\nparity {kind}:", res)
            assert sep_stop_target > 1.0 * 3600.0, "stopped too close to the target to tell"
            assert res["final_radec_err_arcsec"] <= 5.0, res  # lands on the TRACK-moment RA/Dec
            assert to_target > 0.5 * 3600.0  # and the mount did not go back to the old target
            assert_no_false_stops(g)
            await make_stopped(c, pwi)
            await c.close()

        run(go())
    finally:
        g.kill()


def test_track_after_stop_outside_wrap_window_turns_and_lands(pwi, tmp_path):
    """P19 (T3.3b): a goto that changed the axis-0 wrap window and was stopped mid-slew leaves the
    axis outside the new window; a bare mount/tracking_on makes PWI4 turn the axis back into the
    window (intended: it keeps the mount's turning room). After the turn the mount must be
    TRACKING on the RA/Dec of the TRACK moment (<= 5 arcsec), and the safety monitor must not
    have stopped the turn (the Sun is far, altitude stays above Lim EL)."""
    # The real Sun must not matter (and PWI4 cuts slews short near it): run the whole sweep at an
    # altitude >= 30 deg away from the Sun's altitude (the sweep covers all azimuths).
    s0 = pwi.status()
    sun_alt, _ = ephem().sun_altaz(site_of(s0))
    base_alt = 20.0 if abs(20.0 - sun_alt) >= abs(72.0 - sun_alt) else 72.0
    pwi.mount_stop()
    pwi.mount_tracking_off()
    pwi.mount_goto_alt_az(base_alt, s0.mount.azimuth_degs)  # vertical move, then the az sweep
    time.sleep(1.0)
    run(wait_arrived(pwi))
    pwi.mount_set_axis0_wrap_range_min(50.0)  # test infrastructure: axis 0 will read 230.5
    pwi.mount_goto_alt_az(base_alt, 230.5)
    time.sleep(1.0)
    run(wait_arrived(pwi))
    g = start_gateway(tmp_path, SUN_FAR)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 8)
            a0 = (await status(pwi)).mount.axis0.position_degs
            assert abs(a0 - 230.5) < 1.0, a0
            ack = await c.cmd(
                "goto_altaz", {"alt": base_alt + (8.0 if base_alt < 50 else -8.0), "az": 310.56}
            )  # long way: -49.4
            assert ack["ok"], ack
            await asyncio.sleep(1.5)
            assert (await c.cmd("stop"))["ok"]
            await c.wait_state("STOPPED", 8)
            await wait_stationary(pwi)
            res = await track_and_land(c, pwi)
            RESULTS["track_after_stop_outside_wrap"] = res
            print("\nTRACK after a stop outside the wrap window:", res)
            assert res["final_radec_err_arcsec"] <= 5.0, res
            assert_no_false_stops(g)
            await make_stopped(c, pwi)
            await c.close()

        run(go())
    finally:
        g.kill()


# ------------------------------------------------------------------ TRACK refusals


def test_track_refused_below_lim_el_and_inside_sun_disc(pwi, tmp_path):
    # (1) Below Lim EL: the sim is put at alt 10 directly (test infrastructure; Lim EL is 15)
    # (1) Below Lim EL: the pose is at alt 40 and Lim EL is raised to 45 through the gateway's own
    # settings API (its temp settings file; PWI4 is not touched)
    g = start_gateway(tmp_path, SUN_FAR)
    try:

        async def go1():
            import httpx

            from gateway.tests.test_control_pwi4sim import ORIGIN, PORT

            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 8)
            await make_stopped(c, pwi)
            m = (await status(pwi)).mount
            await goto_altaz_and_wait(c, pwi, 40.0, other_az(m.azimuth_degs, 40.0))
            hdr = {"x-mount-token": g.token(), "origin": ORIGIN}
            url = f"http://127.0.0.1:{PORT}/api/mount/settings"
            cur = httpx.get(url, headers=hdr, timeout=3).json()
            body = {k: cur[k] for k in ("version", "location", "hardware", "providers", "display")}
            body["hardware"]["lim_el_deg"] = 45.0
            body["location"]["timezone"] = body["location"]["timezone"] or "UTC"
            r = httpx.put(url, headers=hdr, json=body, timeout=3)
            assert r.status_code == 200, r.text
            ack = await c.cmd("tracking", {"on": True})
            RESULTS["track_refused_below_lim_el"] = ack["reason"]
            assert not ack["ok"]
            assert ack["reason"] == "Below station limit elevation (Lim EL 45°)", ack
            assert not (await status(pwi)).mount.is_tracking
            await c.close()

        run(go1())
    finally:
        g.kill()
    az = (pwi.status().mount.azimuth_degs + 40.0) % 360.0
    # (2) Inside the Sun disc: bring the sim up to alt 50 and pin the Sun 5 deg from it
    pwi.mount_goto_alt_az(50.0, az)
    time.sleep(1.0)
    run(wait_stationary(pwi))
    pwi.mount_tracking_off()
    m = pwi.status().mount
    g = start_gateway(tmp_path, f"{m.altitude_degs + 5.0:.3f},{m.azimuth_degs:.3f}")
    try:

        async def go3():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 8)
            ack = await c.cmd("tracking", {"on": True})
            RESULTS["track_refused_sun_disc"] = ack["reason"]
            assert not ack["ok"] and ack["reason"] == "Within 15° of the Sun", ack
            assert not (await status(pwi)).mount.is_tracking
            await c.close()

        run(go3())
    finally:
        g.kill()
    refusals = [e for e in g.audit_events() if e["event"] == "safety_refusal"]
    assert refusals and all(e["cmd"] == "tracking" for e in refusals)


# ------------------------------------------------------------------ not-homed guard


def test_not_homed_guard_connect_with_home_blocked_then_override(pwi, tmp_path):
    """CONNECT with the home pose inside the (pinned) Sun disc skips the home: the mount is
    not_homed, GOTO / TRACK are refused, homed_override lifts it, disconnect ends the session."""
    disconnect_sim(pwi)
    g = start_gateway(tmp_path, f"{HOME_ALT - 5.0},{HOME_AZ}")
    try:

        async def go():
            c = await SeqClient.open(g.token())
            await c.wait_state("DISCONNECTED", 8)
            assert c.homed is None
            ack = await c.cmd("connect")
            assert ack["ok"], ack
            seq = await c.wait_sequence(40, "connect")
            assert seq["status"] == "done" and seq["warning"], seq
            await c.wait_state("STOPPED", 5)
            for _ in range(100):
                if c.homed:
                    break
                await asyncio.sleep(0.05)
            assert c.homed and c.homed["state"] == "not_homed", c.homed
            for name, args in [
                ("goto_altaz", {"alt": 50.0, "az": 270.0}),
                ("goto_radec_j2000", {"ra_h": 3.0, "dec_deg": 10.0}),
                ("tracking", {"on": True}),
            ]:
                ack = await c.cmd(name, args)
                assert not ack["ok"] and ack["reason"] == "Mount not homed", (name, ack)
            m = (await status(pwi)).mount
            assert not m.is_tracking and not m.is_slewing

            ack = await c.cmd("homed_override")
            assert ack["ok"], ack
            for _ in range(100):
                if c.homed and c.homed["state"] == "overridden":
                    break
                await asyncio.sleep(0.05)
            assert c.homed["state"] == "overridden"
            # no longer refused as "not homed" (any other gate, e.g. the Sun, is still allowed)
            ack = await c.cmd("goto_altaz", {"alt": 50.0, "az": 270.0})
            RESULTS["homed_override_goto"] = {"ok": ack["ok"], "reason": ack["reason"]}
            assert ack["reason"] != "Mount not homed"
            await make_stopped(c, pwi)
            again = await c.cmd("homed_override")
            assert not again["ok"]

            # disconnect ends the session (home is Sun-blocked: confirm the second send)
            ack = await c.cmd("disconnect")
            if not ack["ok"]:
                ack = await c.cmd("disconnect", {"skip_home": True})
            assert ack["ok"], ack
            await c.wait_sequence(40, "disconnect")
            await c.wait_state("DISCONNECTED", 8)
            for _ in range(100):
                if c.homed is None:
                    break
                await asyncio.sleep(0.05)
            assert c.homed is None
            await c.close()

        run(go())
    finally:
        g.kill()
    ev = [e["event"] for e in g.audit_events()]
    assert "homed_session_start" in ev and "homed_override" in ev
    causes = [e["cause"] for e in g.audit_events() if e["event"] == "homed_session_start"]
    assert causes == ["connect_sequence"], causes
    disconnect_sim(pwi)


def test_not_homed_guard_cleared_by_a_completed_home(pwi, tmp_path):
    """A mount found connected is not_homed (Q4); a COMPLETED home (the sim's no-op home counts)
    clears it and GOTO / TRACK work. The sim is not exempt."""
    disconnect_sim(pwi)
    connect_sim(pwi)
    g = start_gateway(tmp_path, SUN_FAR)
    try:

        async def go():
            c = await SeqClient.open(g.token(), override_homed=False)
            await c.wait_state("STOPPED", 8)
            for _ in range(100):
                if c.homed:
                    break
                await asyncio.sleep(0.05)
            assert c.homed and c.homed["state"] == "not_homed", c.homed
            ack = await c.cmd("goto_altaz", {"alt": 50.0, "az": 200.0})
            assert not ack["ok"] and ack["reason"] == "Mount not homed", ack
            # always allowed while not homed: STOP, tracking off
            assert (await c.cmd("stop"))["ok"]
            assert (await c.cmd("tracking", {"on": False}))["ok"]

            ack = await c.cmd("home")
            assert ack["ok"], ack
            seq = await c.wait_sequence(40, "home")
            assert seq["status"] == "done", seq
            await c.wait_state("STOPPED", 8)
            for _ in range(100):
                if c.homed and c.homed["state"] == "homed":
                    break
                await asyncio.sleep(0.05)
            assert c.homed["state"] == "homed", c.homed
            await goto_altaz_and_wait(c, pwi, 55.0, 200.0)
            ack = await c.cmd("tracking", {"on": True})
            assert ack["ok"], ack
            await c.wait_state("TRACKING", 5)
            await make_stopped(c, pwi)
            await c.close()

        run(go())
    finally:
        g.kill()
    ev = g.audit_events()
    assert [e["cause"] for e in ev if e["event"] == "homed_session_start"] == ["gateway_start"]
    assert [e["how"] for e in ev if e["event"] == "homed"] == ["no_motion"]
    disconnect_sim(pwi)
