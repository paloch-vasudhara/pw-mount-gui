# ruff: noqa: F811
"""T3.5c: D-SUN routes against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

The Sun is PINNED with the test-only `PW_TEST_SUN_ALTAZ` on the mount's own altitude, half-way
along an 80 deg azimuth sweep, so every run crosses the disc whatever the real time of day. The
harness (throwaway gateway on its own port, `pwi` fixture with `sim_guard.assert_simulator()`) is
the one of the other sim tests.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import UTC, datetime
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
)
from .test_jog_pwi4sim import JogClient, open_client, put_pose
from .test_safety_pwi4sim import set_lim_el

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]


def prepare_pose(pwi, alt: float) -> tuple[float, float, float]:
    """Direct PWI4 (test infrastructure): stop, tracking off, point at `alt` on the current
    azimuth and wait until the mount stands still. Returns (axis0, alt, sweep sign)."""
    pwi.mount_stop()
    pwi.mount_tracking_off()
    time.sleep(1.0)
    m = pwi.status().mount
    pwi.mount_goto_alt_az(alt, m.azimuth_degs)
    t0, last = time.monotonic(), None
    while time.monotonic() - t0 < 40:
        m = pwi.status().mount
        pos = (m.axis0.position_degs, m.axis1.position_degs)
        if last is not None and max(abs(pos[0] - last[0]), abs(pos[1] - last[1])) < 0.002:
            if abs(pos[1] - alt) < 0.05:
                break
        last = pos
        time.sleep(0.4)
    m = pwi.status().mount
    a0 = m.axis0.position_degs
    return a0, m.axis1.position_degs, (-1.0 if a0 > 100.0 else 1.0)


def start_gateway(tmp_path, sun: str) -> Gateway:
    g = Gateway(tmp_path)
    g.extra_env = {"PW_TEST_SUN_ALTAZ": sun}
    g.start()
    return g


def sep_to(sun: tuple[float, float], axis0: float, alt: float) -> float:
    return angular_sep_deg(axis0 % 360.0, alt, sun[1], sun[0])


def events(g: Gateway, name: str) -> list[dict]:
    return [e for e in g.audit_events() if e["event"] == name]


async def wait_event(g: Gateway, name: str, timeout: float) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ev = events(g, name)
        if ev:
            return ev[-1]
        await asyncio.sleep(0.2)
    raise AssertionError(f"no {name} audit event within {timeout} s")


def test_local_goto_through_the_sun_is_routed(pwi, tmp_path):
    a0, alt0, sign = prepare_pose(pwi, 45.0)
    sun = (alt0, (a0 + sign * 40.0) % 360.0)
    tgt_az = (a0 + sign * 80.0) % 360.0
    g = start_gateway(tmp_path, f"{sun[0]:.4f},{sun[1]:.4f}")
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            await asyncio.sleep(1.0)
            smp = Sampler(pwi).start()
            t_cmd = time.monotonic()
            ack = await c.cmd("goto_altaz", {"alt": alt0, "az": tgt_az})
            assert ack["ok"], ack
            t_ack = time.monotonic() - t_cmd
            await c.wait_state("SLEWING", 5)
            await asyncio.sleep(0.5)
            reasons = c.state
            await wait_event(g, "sun_detour_done", 120)
            await c.wait_state("STOPPED", 30)
            await asyncio.sleep(1.5)
            smp.close()
            m = pwi.status().mount
            await c.close()
            return smp.samples, m, t_ack, reasons

        samples, m, t_ack, state_mid = run(go())
        det = events(g, "sun_detour")[0]
        seps = [sep_to(sun, s[1], s[2]) for s in samples]
        moving = [
            s[0]
            for a, s in zip(samples, samples[1:], strict=False)
            if max(abs(s[1] - a[1]), abs(s[2] - a[2])) / max(s[0] - a[0], 1e-3) > 0.05
        ]
        # the path part: motion between the first motion and the end of the path hold
        t_first = moving[0]
        err_alt = abs(m.axis1.position_degs - alt0)
        err_az = abs((m.axis0.position_degs - tgt_az + 180.0) % 360.0 - 180.0)
        # route time: from the first motion to the last motion before the final goto starts is
        # not separable from the samples alone; use the whole motion span minus the final slew
        span = moving[-1] - t_first
        plan_total = det["duration_s"] + 2.0  # + settle wait; the final slew is a few degrees
        RESULTS["dsun_local_goto"] = {
            "min_sun_sep_deg": round(min(seps), 3),
            "plan_min_sun_sep": det["min_sun_sep"],
            "arrival_err_deg": [round(err_az, 4), round(err_alt, 4)],
            "route_plan_s": det["duration_s"],
            "motion_span_s": round(span, 2),
            "ack_s": round(t_ack, 2),
            "state_mid_route": state_mid,
            "tracking_after": bool(m.is_tracking),
            "sides": det["sides"],
        }
        print("DSUN local:", RESULTS["dsun_local_goto"])
        assert min(seps) >= 15.0, min(seps)
        assert state_mid == "SLEWING"
        assert max(err_az, err_alt) < 0.02, (err_az, err_alt)  # the final goto's arrival
        assert 0.7 * plan_total <= span <= 1.3 * plan_total + 3.0, (span, plan_total)
        assert not m.is_tracking and not m.is_slewing
        assert det["sides"][0] in ("cw", "ccw") and det["min_sun_sep"] >= 15.9
    finally:
        g.kill()


def test_celestial_goto_routed_then_tracking(pwi, tmp_path):
    a0, alt0, sign = prepare_pose(pwi, 45.0)
    sun = (alt0, (a0 + sign * 40.0) % 360.0)
    tgt_az = (a0 + sign * 80.0) % 360.0
    s = pwi.status()
    site = (s.site.latitude_degs, s.site.longitude_degs, s.site.height_meters)
    ra_h, dec = Ephem(ROOT / "data" / "ephemeris" / "de421.bsp").altaz_radec_j2000(
        site, alt0, tgt_az, datetime.now(UTC)
    )
    g = start_gateway(tmp_path, f"{sun[0]:.4f},{sun[1]:.4f}")
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            await asyncio.sleep(1.0)
            smp = Sampler(pwi).start()
            ack = await c.cmd("goto_radec_j2000", {"ra_h": ra_h, "dec_deg": dec})
            assert ack["ok"], ack
            await wait_event(g, "sun_detour_done", 120)
            await c.wait_state("TRACKING", 40)
            await asyncio.sleep(2.0)
            smp.close()
            st = c.state
            m = pwi.status().mount
            await c.close()
            return smp.samples, m, st

        samples, m, st = run(go())
        seps = [sep_to(sun, s[1], s[2]) for s in samples]
        err = angular_sep_deg(m.azimuth_degs, m.altitude_degs, tgt_az, alt0)
        RESULTS["dsun_celestial_goto"] = {
            "min_sun_sep_deg": round(min(seps), 3),
            "final_state": st,
            "pointing_err_deg": round(err, 3),
        }
        print("DSUN radec:", RESULTS["dsun_celestial_goto"])
        assert min(seps) >= 15.0 and st == "TRACKING" and m.is_tracking
        assert err < 0.5  # the target drifts with the sky during the route; tracking follows it
    finally:
        g.kill()


def test_stop_mid_route_freezes_and_sends_no_final_goto(pwi, tmp_path):
    a0, alt0, sign = prepare_pose(pwi, 45.0)
    sun = (alt0, (a0 + sign * 40.0) % 360.0)
    tgt_az = (a0 + sign * 80.0) % 360.0
    g = start_gateway(tmp_path, f"{sun[0]:.4f},{sun[1]:.4f}")
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            await asyncio.sleep(1.0)
            smp = Sampler(pwi).start()
            ack = await c.cmd("goto_altaz", {"alt": alt0, "az": tgt_az})
            assert ack["ok"], ack
            t0 = time.monotonic()
            while time.monotonic() - t0 < 15:  # the path starts after the LEAD
                v = smp.speeds()
                if v and v[-1][1] > 2.0:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(2.0)  # well inside the route
            t_stop = time.monotonic()
            ack = await c.cmd("stop")
            assert ack["ok"], ack
            await asyncio.sleep(4.0)
            smp.close()
            frozen = smp.stopped_at(t_stop)
            pos = [(s[1], s[2]) for s in smp.samples if s[0] > t_stop + 1.0]
            drift = max(max(abs(p[0] - pos[0][0]), abs(p[1] - pos[0][1])) for p in pos)
            m = pwi.status().mount
            await c.close()
            return t_stop, frozen, drift, m, c.state

        t_stop, frozen, drift, m, st = run(go())
        latency = None if frozen is None else frozen - t_stop
        RESULTS["dsun_stop_mid_route"] = {
            "stop_to_frozen_s": None if latency is None else round(latency, 3),
            "drift_after_1s_deg": round(drift, 4),
            "state": st,
        }
        print("DSUN stop:", RESULTS["dsun_stop_mid_route"])
        assert latency is not None and latency <= 0.3, latency
        assert drift < 0.01  # no final goto / no further path motion
        assert not m.is_tracking
        stopped = events(g, "sun_detour_stopped")
        assert len(stopped) == 1 and "STOP" in stopped[0]["reason"]
        assert not events(g, "sun_detour_done")
        # the final goto was never sent: the mount is nowhere near the target
        assert abs((m.axis0.position_degs - tgt_az + 180.0) % 360.0 - 180.0) > 5.0
    finally:
        g.kill()


def test_no_detour_is_refused(pwi, tmp_path):
    a0, alt0, sign = prepare_pose(pwi, 75.0)
    sun = (alt0, (a0 + sign * 90.0) % 360.0)
    # 170, not 180 (supervisor fix): at exactly 180 both directions are equally near, PWI4 may
    # sweep the other way (away from the Sun) and the slew is then correctly a plain goto.
    tgt_az = (a0 + sign * 170.0) % 360.0
    g = start_gateway(tmp_path, f"{sun[0]:.4f},{sun[1]:.4f}")
    try:

        async def go():
            c = await Client.open(g.token())
            await c.wait_state("STOPPED", 5)
            set_lim_el(
                g, 60.0
            )  # the low side (alt 59) is below Lim EL + pad, the high one over the keyhole
            await asyncio.sleep(0.8)
            before = pwi.status().mount
            ack = await c.cmd("goto_altaz", {"alt": alt0, "az": tgt_az})
            await asyncio.sleep(1.5)
            after = pwi.status().mount
            await c.close()
            return ack, before, after

        ack, before, after = run(go())
        RESULTS["dsun_no_detour"] = {"reason": ack["reason"]}
        assert ack["ok"] is False
        assert ack["reason"] == "Slew path passes within 15° of the Sun; no safe detour"
        assert math.isclose(before.axis0.position_degs, after.axis0.position_degs, abs_tol=0.05)
        assert not events(g, "sun_detour")
    finally:
        g.kill()


# ------------------------------------------------------------------ T3.5e: wrap_turn and HOME


def set_home(g: Gateway, alt: float, az: float) -> None:
    """Change the home pose through the real settings API (throwaway settings file)."""
    h = {"x-mount-token": g.token(), "origin": ORIGIN}
    base = f"http://127.0.0.1:{PORT}/api/mount/settings"
    cur = httpx.get(base, headers=h, timeout=3).json()
    cur.pop("api_key_set", None)
    cur["hardware"]["home_alt_deg"] = alt
    cur["hardware"]["home_az_deg"] = az
    cur["location"]["timezone"] = cur["location"].get("timezone") or "UTC"
    r = httpx.put(base, headers=h, json=cur, timeout=3)
    assert r.status_code == 200, r.text


def test_wrap_turn_is_routed(pwi, tmp_path):
    """Jog to the axis-0 mechanical limit (prompt), then a 360 deg turn with the Sun pinned on
    its altitude circle (alt 50, az 150): the sweep is routed around the disc."""
    # the start pose first: with the Sun already pinned the gateway would stop the way there
    run(put_pose(pwi, 50.0, 255.0))
    g = start_gateway(tmp_path, "50,150")
    sun = (50.0, 150.0)
    try:

        async def go():
            c = await open_client(g)
            args = {"axis": 0, "dir": 1, "speed": 10.0}
            assert (await c.cmd("jog", args))["ok"]
            c.keepalive(args)
            await c.until(lambda: c.jog is None and c.jog_limit is not None, 12.0, "limit stop")
            await c.stop_keepalive()
            await asyncio.sleep(1.5)
            m1 = pwi.status().mount
            lims = (m1.axis0.min_mech_position_degs, m1.axis0.max_mech_position_degs)
            a0, alt0, az0 = m1.axis0.position_degs, m1.altitude_degs, m1.azimuth_degs
            smp = Sampler(pwi).start()
            t_cmd = time.monotonic()
            ack = await c.cmd("wrap_turn", {})
            assert ack["ok"], ack
            t_ack = time.monotonic() - t_cmd
            await wait_event(g, "sun_detour", 10)
            await asyncio.sleep(3.0)
            state_mid = c.state
            await wait_event(g, "sun_detour_done", 240)
            await c.until(lambda: c.turn is None and c.state == "STOPPED", 120.0, "turn done")
            took = time.monotonic() - t_cmd
            await asyncio.sleep(1.5)
            smp.close()
            m2 = pwi.status().mount
            await c.close()
            return smp.samples, a0, alt0, az0, lims, m2, state_mid, took, t_ack

        try:
            samples, a0, alt0, az0, lims, m2, state_mid, took, t_ack = run(go())
        finally:
            try:
                pwi.mount_stop()
                pwi.mount_tracking_off()
                pwi.mount_set_axis0_wrap_range_min(-180.0)
            except Exception:
                pass
        det = events(g, "sun_detour")[0]
        seps = [sep_to(sun, s[1], s[2]) for s in samples]
        x = [s[1] for s in samples]
        worst_back = max(x[i] - min(x[: i + 1]) for i in range(len(x)))  # reversal vs descent
        d_az = abs((m2.azimuth_degs - az0 + 180.0) % 360.0 - 180.0)
        d_alt = abs(m2.altitude_degs - alt0)
        d_ax0 = abs(m2.axis0.position_degs - (a0 - 360.0))
        RESULTS["dsun_wrap_turn"] = {
            "min_sun_sep_deg": round(min(seps), 3),
            "plan_min_sun_sep": det["min_sun_sep"],
            "kind": det["kind"],
            "truncated": det["truncated"],
            "sides": det["sides"],
            "route_plan_s": det["duration_s"],
            "total_s": round(took, 1),
            "ack_s": round(t_ack, 2),
            "end_err_az_alt_axis0_deg": [round(d_az, 4), round(d_alt, 4), round(d_ax0, 4)],
            "axis0_range_seen": [round(min(x), 2), round(max(x), 2)],
            "worst_reversal_deg": round(worst_back, 2),
            "state_mid_route": state_mid,
        }
        print("DSUN wrap_turn:", RESULTS["dsun_wrap_turn"])
        assert det["kind"] == "wrap_turn" and det["truncated"] is True
        assert state_mid == "SLEWING"
        assert min(seps) >= 15.0, min(seps)
        assert max(d_az, d_alt) < 0.02, (d_az, d_alt)
        assert d_ax0 < 0.05, d_ax0
        assert not m2.is_tracking and not m2.is_slewing
        assert min(x) >= lims[0] and max(x) <= lims[1], (min(x), max(x), lims)
        # the detour arc around the Sun may back up in azimuth by a few degrees (a 16 deg circle
        # at alt 50 spans +-25 deg of azimuth); an unwinding sweep would be hundreds
        assert worst_back <= 6.0, worst_back
        assert min(x) >= a0 - 360.0 - 6.0
        assert events(g, "sun_detour_done")
        assert [e["how"] for e in events(g, "wrap_turn_end")] == ["done"]
    finally:
        g.kill()


def _home_geometry(pwi) -> tuple[float, float, float, float]:
    """(start axis0, alt, home az, sweep sign): a start pose at alt 50 and a home pose 100 deg of
    azimuth away, so the pinned Sun half-way sits on the home path."""
    a0, alt0, sign = prepare_pose(pwi, 50.0)
    return a0, alt0, (a0 + sign * 100.0) % 360.0, sign


def test_home_is_routed_when_homed(pwi, tmp_path):
    a0, alt0, home_az, sign = _home_geometry(pwi)
    sun = (alt0, (a0 + sign * 50.0) % 360.0)
    g = start_gateway(tmp_path, f"{sun[0]:.4f},{sun[1]:.4f}")
    try:

        async def go():
            c = await JogClient.open(g.token(), override_homed=False)
            await c.wait_state("STOPPED", 5)
            await c.until(lambda: c.homed and c.homed["state"] == "not_homed", 5.0, "not_homed")
            # 1) a completed home from a pose whose path is clear: home pose = the start pose
            set_home(g, round(alt0, 2), round(a0 % 360.0, 2))
            await asyncio.sleep(0.8)
            assert (await c.cmd("home"))["ok"]
            await c.until(lambda: c.homed["state"] == "homed", 30.0, "homed")
            await c.wait_state("STOPPED", 10)
            n_det = len(events(g, "sun_detour"))
            # 2) the home pose moves 100 deg away: the Sun sits on the way
            set_home(g, round(alt0, 2), round(home_az, 2))
            await asyncio.sleep(0.8)
            smp = Sampler(pwi).start()
            ack = await c.cmd("home")
            assert ack["ok"], ack
            t0 = time.monotonic()
            await wait_event(g, "sun_detour", 10)
            await asyncio.sleep(3.0)
            state_mid = c.state
            while len(events(g, "home_finished")) < 2 and time.monotonic() - t0 < 120:
                await asyncio.sleep(0.2)
            took = time.monotonic() - t0
            await c.wait_state("STOPPED", 20)
            await asyncio.sleep(1.5)
            smp.close()
            m = pwi.status().mount
            hs = c.homed
            await c.close()
            return smp.samples, m, hs, n_det, state_mid, took

        samples, m, hs, n_det, state_mid, took = run(go())
        ev = g.audit_events()
        dets = [e for e in ev if e["event"] == "sun_detour"]
        seps = [sep_to(sun, s[1], s[2]) for s in samples]
        err_az = abs((m.azimuth_degs - home_az + 180.0) % 360.0 - 180.0)
        err_alt = abs(m.altitude_degs - round(alt0, 2))
        RESULTS["dsun_home"] = {
            "min_sun_sep_deg": round(min(seps), 3),
            "detours": [(e["kind"], e["sides"], e["duration_s"]) for e in dets],
            "home_finished": [e["how"] for e in ev if e["event"] == "home_finished"],
            "end_err_az_alt_deg": [round(err_az, 4), round(err_alt, 4)],
            "homed_state": hs["state"],
            "state_mid_route": state_mid,
            "total_s": round(took, 1),
        }
        print("DSUN home:", RESULTS["dsun_home"])
        assert n_det == 0 and len(dets) == 1 and dets[0]["kind"] == "home", dets
        assert min(seps) >= 15.0, min(seps)
        assert len(events(g, "home_finished")) == 2
        assert events(g, "sun_detour_done")
        assert [e["how"] for e in events(g, "homed")] == ["no_motion", "no_motion"]
        assert max(err_az, err_alt) < 0.3, (err_az, err_alt)
        assert hs["state"] == "homed"
        assert not m.is_tracking and not m.is_slewing
    finally:
        g.kill()
        try:
            pwi.mount_stop()
            pwi.mount_tracking_off()
        except Exception:
            pass


def test_home_not_routed_when_overridden(pwi, tmp_path):
    a0, alt0, home_az, sign = _home_geometry(pwi)
    sun = (alt0, (a0 + sign * 50.0) % 360.0)
    g = start_gateway(tmp_path, f"{sun[0]:.4f},{sun[1]:.4f}")
    try:

        async def go():
            c = await open_client(g)  # homed_override, not a completed home
            set_home(g, round(alt0, 2), round(home_az, 2))
            await asyncio.sleep(0.8)
            before = pwi.status().mount
            ack = await c.cmd("home")
            await asyncio.sleep(2.0)
            after = pwi.status().mount
            await c.close()
            return ack, before, after

        ack, before, after = run(go())
        RESULTS["dsun_home_overridden"] = {"reason": ack["reason"]}
        assert ack["ok"] is False
        assert ack["reason"] == "Slew path passes within 15° of the Sun", ack
        assert math.isclose(before.axis0.position_degs, after.axis0.position_degs, abs_tol=0.05)
        assert not events(g, "sun_detour") and not events(g, "home_finished")
    finally:
        g.kill()
