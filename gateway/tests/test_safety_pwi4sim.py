# ruff: noqa: F811
"""T3.1b gate measurements against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

Reuses the throwaway-gateway / WebSocket helpers of the T3.1a sim tests (own port, temp settings
and audit dir). `sim_guard.assert_simulator()` runs in the shared `pwi` fixture before any PWI4
command. Only targets above the horizon are ever commanded (P14). Sun-crossing cases use the
test-only `PW_TEST_SUN_ALTAZ` override and are REFUSALS (nothing moves); the real Sun is never
approached.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from gateway.ephem import Ephem, angular_sep_deg

from .test_control_pwi4sim import (  # noqa: F401  (pwi is a fixture)
    ORIGIN,
    PORT,
    RESULTS,
    Client,
    Gateway,
    Sampler,
    pwi,
    run,
    safe_target,
)

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]


def start_gateway(tmp_path, sun: str = "-60,0") -> Gateway:
    g = Gateway(tmp_path)
    g.extra_env = {"PW_TEST_SUN_ALTAZ": sun}
    g.start()
    return g


def set_lim_el(g: Gateway, value: float) -> None:
    """Change Lim EL through the real settings API (throwaway settings file)."""
    h = {"x-mount-token": g.token(), "origin": ORIGIN}
    base = f"http://127.0.0.1:{PORT}/api/mount/settings"
    cur = httpx.get(base, headers=h, timeout=3).json()
    cur.pop("api_key_set", None)
    cur["hardware"]["lim_el_deg"] = value
    cur["location"]["timezone"] = cur["location"].get("timezone") or "UTC"
    r = httpx.put(base, headers=h, json=cur, timeout=3)
    assert r.status_code == 200, r.text


def mount_pos(pwi):
    m = pwi.status().mount
    return m.axis0.position_degs, m.axis1.position_degs, m.altitude_degs, m.azimuth_degs


async def goto_and_settle(c: Client, pwi, alt: float, az: float, timeout: float = 40.0) -> None:
    ack = await c.cmd("goto_altaz", {"alt": alt, "az": az})
    assert ack["ok"], ack
    await asyncio.sleep(0.6)
    await c.wait_state("STOPPED", timeout)
    await asyncio.sleep(0.5)


def test_radec_to_altaz_matches_pwi4(pwi):
    """Validate the J2000 -> apparent alt/az conversion against PWI4's own reading."""
    alt, az = safe_target(pwi, 40.0)
    pwi.mount_goto_alt_az(alt, az)
    time.sleep(6.0)
    s = pwi.status()
    m = s.mount
    when = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(days=m.julian_date - 2440587.5)
    eph = Ephem(ROOT / "data" / "ephemeris" / "de421.bsp")
    site = (s.site.latitude_degs, s.site.longitude_degs, s.site.height_meters)
    c_alt, c_az = eph.radec_j2000_altaz(site, m.ra_j2000_hours, m.dec_j2000_degs, when)
    sep = angular_sep_deg(c_az, c_alt, m.azimuth_degs, m.altitude_degs)
    RESULTS["radec_conversion"] = {
        "pwi4_altaz": [round(m.altitude_degs, 4), round(m.azimuth_degs, 4)],
        "skyfield_altaz": [round(c_alt, 4), round(c_az, 4)],
        "sep_deg": round(sep, 4),
    }
    pwi.mount_stop()
    assert sep < 0.05, sep


def test_refusals_leave_sim_untouched(pwi, tmp_path):
    g = start_gateway(tmp_path)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            await asyncio.sleep(1.0)
            before = mount_pos(pwi)
            out = {}
            out["below_lim_el"] = (await c.cmd("goto_altaz", {"alt": 10.0, "az": 100.0}))["reason"]
            ra_dec = pwi.status().mount
            # a J2000 position 12 h from where the mount points: far below the horizon
            out["radec_below_horizon"] = (
                await c.cmd(
                    "goto_radec_j2000",
                    {
                        "ra_h": (ra_dec.ra_j2000_hours + 12.0) % 24.0,
                        "dec_deg": -ra_dec.dec_j2000_degs,
                    },
                )
            )["reason"]
            await asyncio.sleep(1.5)
            after = mount_pos(pwi)
            moved = max(abs(a - b) for a, b in zip(before[:2], after[:2], strict=True))
            out["moved_deg"] = round(moved, 4)
            RESULTS["sim_refusals"] = out
            await c.close()
            return out, moved

        out, moved = run(go())
        assert out["below_lim_el"].startswith("Below station limit elevation")
        assert out["radec_below_horizon"].startswith("Below station limit elevation")
        assert moved < 0.05  # nothing moved: no write reached PWI4
        ev = [e for e in g.audit_events() if e["event"] == "safety_refusal"]
        assert len(ev) == 2
    finally:
        g.kill()


def test_sun_refusals_with_test_override(pwi, tmp_path):
    alt, az = safe_target(pwi, 60.0)  # also guarantees a real-Sun-safe sweep for the allowed move
    cur = pwi.status().mount
    cur_az, cur_alt = cur.azimuth_degs, cur.altitude_degs
    # Sun placed 30 deg ahead on the way to the first target, at the mount's altitude
    sun = f"{cur_alt:.3f},{(cur_az + 30.0) % 360.0:.3f}"
    g = start_gateway(tmp_path, sun)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            await asyncio.sleep(1.0)
            before = mount_pos(pwi)
            out = {}
            out["target_in_disc"] = (
                await c.cmd(
                    "goto_altaz", {"alt": cur_alt, "az": (cur_az + 35.0) % 360.0}, timeout=6
                )
            )["reason"]
            out["path_crossing"] = (
                await c.cmd("goto_altaz", {"alt": cur_alt, "az": (cur_az + 80.0) % 360.0})
            )["reason"]
            out["tracking_in_disc_n/a"] = None
            await asyncio.sleep(1.0)
            moved = max(abs(a - b) for a, b in zip(before[:2], mount_pos(pwi)[:2], strict=True))
            out["moved_deg"] = round(moved, 4)
            RESULTS["sim_sun_refusals"] = {"sun_override": sun, **out}
            await c.close()
            return out, moved

        out, moved = run(go())
        assert out["target_in_disc"] == "Within 15° of the Sun"
        assert out["path_crossing"] == "Slew path passes within 15° of the Sun"
        assert moved < 0.05
    finally:
        g.kill()


def test_monitor_stops_external_slew_at_lim_el(pwi, tmp_path):
    """Motion started OUTSIDE the app (PWI4 client): the 10 Hz monitor stops it before Lim EL."""
    g = start_gateway(tmp_path)
    lim = 30.0
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            alt0, az = safe_target(pwi, 20.0)
            await goto_and_settle(c, pwi, 50.0, az)  # alt 50 start (above Lim EL)
            set_lim_el(g, lim)
            await asyncio.sleep(0.5)
            smp = Sampler(pwi).start()
            t0 = time.monotonic()
            pwi.mount_goto_alt_az(20.0, az)  # outside the app: heads below Lim EL (above horizon)
            await c.wait_state("SLEWING", 5)
            await c.wait_state("STOPPED", 10)
            t_stop = time.monotonic() - t0
            await asyncio.sleep(0.8)
            smp.close()
            final_alt = pwi.status().mount.altitude_degs
            lowest = min(s[2] for s in smp.samples)
            # the commands that would push further are refused afterwards
            refused = (await c.cmd("goto_altaz", {"alt": 20.0, "az": az}))["reason"]
            RESULTS["monitor_external_slew"] = {
                "lim_el": lim,
                "start_alt": 50.0,
                "final_alt": round(final_alt, 3),
                "lowest_alt_seen": round(lowest, 3),
                "margin_above_lim_deg": round(final_alt - lim, 3),
                "overshoot_below_lim_deg": round(max(0.0, lim - lowest), 3),
                "time_to_stopped_s": round(t_stop, 2),
                "refusal_afterwards": refused,
            }
            await c.close()
            return final_alt, lowest, refused

        final_alt, lowest, refused = run(go())
        assert lowest >= lim - 0.05, (lowest, lim)
        assert refused.startswith("Below station limit elevation")
        ev = [e for e in g.audit_events() if e["event"] == "limit_stop"]
        assert ev and ev[0]["reason"].startswith("Below station limit elevation")
        RESULTS["monitor_external_slew"]["audit"] = ev[0]
    finally:
        g.kill()


def test_monitor_stops_setting_sidereal_track(pwi, tmp_path):
    """Sidereal track of a setting direction with Lim EL a hair below the pointing: the monitor
    must stop it at the limit."""
    g = start_gateway(tmp_path)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            await asyncio.sleep(0.5)
            # western sky, away from the (real) Sun: pointing >= 35 deg from it is enforced below
            s = pwi.status()
            eph = Ephem(ROOT / "data" / "ephemeris" / "de421.bsp")
            sun = eph.update((s.site.latitude_degs, s.site.longitude_degs, s.site.height_meters))
            az = 290.0
            assert angular_sep_deg(az, 40.0, sun.sun_az, sun.sun_alt) > 35.0, "Sun too close"
            await goto_and_settle(c, pwi, 40.0, az)
            alt_now = pwi.status().mount.altitude_degs
            lim = round(alt_now - 0.12, 3)
            set_lim_el(g, lim)
            await asyncio.sleep(0.5)
            ack = await c.cmd("tracking", {"on": True})
            assert ack["ok"], ack
            await c.wait_state("TRACKING", 5)
            t0 = time.monotonic()
            samples = []
            while time.monotonic() - t0 < 120:
                m = pwi.status().mount
                samples.append((time.monotonic() - t0, m.altitude_degs, bool(m.is_tracking)))
                if c.state == "STOPPED":
                    break
                await asyncio.sleep(0.2)
            t_stop = time.monotonic() - t0
            await asyncio.sleep(1.0)
            m = pwi.status().mount
            RESULTS["monitor_sidereal_track"] = {
                "lim_el": lim,
                "alt_at_track_start": round(alt_now, 4),
                "final_alt": round(m.altitude_degs, 4),
                "margin_above_lim_deg": round(m.altitude_degs - lim, 4),
                "time_to_stop_s": round(t_stop, 1),
                "tracking_after": bool(m.is_tracking),
            }
            await c.close()
            return m.altitude_degs, lim, t_stop, bool(m.is_tracking)

        final_alt, lim, t_stop, trk = run(go())
        assert c_ok(final_alt, lim)
        assert t_stop < 119 and not trk
        ev = [e for e in g.audit_events() if e["event"] == "limit_stop"]
        assert ev and ev[0]["reason"].startswith("Below station limit elevation")
    finally:
        g.kill()


def c_ok(final_alt: float, lim: float) -> bool:
    """Stopped at or just above the limit, never meaningfully below it."""
    return final_alt >= lim - 0.02


@pytest.mark.parametrize("margin", [0.5, 0.1])
def test_validated_goto_reaches_lim_el_plus_margin(pwi, tmp_path, margin):
    """Regression (supervisor gate): a legal descent from alt 85 to Lim EL + margin must run to
    its target; the monitor must not cut it short with linear extrapolation."""
    g = start_gateway(tmp_path)
    lim = 15.0
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            _, az = safe_target(pwi, 20.0)
            await goto_and_settle(c, pwi, 85.0, az)
            smp = Sampler(pwi).start()
            ack = await c.cmd("goto_altaz", {"alt": lim + margin, "az": az})
            assert ack["ok"], ack
            await asyncio.sleep(0.6)
            await c.wait_state("STOPPED", 30)
            await asyncio.sleep(0.8)
            smp.close()
            m = pwi.status().mount
            lowest = min(s[2] for s in smp.samples)
            RESULTS[f"validated_descent_margin_{margin}"] = {
                "target_alt": lim + margin,
                "final_alt": round(m.altitude_degs, 4),
                "lowest_alt_seen": round(lowest, 4),
                "tracking": bool(m.is_tracking),
            }
            await c.close()
            return m.altitude_degs, lowest

        final_alt, lowest = run(go())
        assert abs(final_alt - (lim + margin)) <= 0.1, final_alt
        assert lowest >= lim
        assert not [e for e in g.audit_events() if e["event"] == "limit_stop"]
    finally:
        g.kill()


def test_p15_goto_altaz_ends_tracking(pwi, tmp_path):
    g = start_gateway(tmp_path)
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            _, az = safe_target(pwi, 20.0)
            await goto_and_settle(c, pwi, 45.0, az)
            assert (await c.cmd("tracking", {"on": True}))["ok"]
            await c.wait_state("TRACKING", 5)
            ack = await c.cmd("goto_altaz", {"alt": 50.0, "az": az})
            assert ack["ok"], ack
            await asyncio.sleep(0.6)
            await c.wait_state("STOPPED", 30)
            await asyncio.sleep(0.8)
            m = pwi.status().mount
            RESULTS["p15_goto_altaz"] = {
                "state_after": c.state,
                "is_tracking": bool(m.is_tracking),
                "final_alt": round(m.altitude_degs, 3),
            }
            await c.close()
            return c.state, bool(m.is_tracking)

        state, tracking = run(go())
        assert state == "STOPPED" and not tracking
    finally:
        g.kill()
