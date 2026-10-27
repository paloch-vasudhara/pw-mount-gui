# ruff: noqa: F811
"""T3.3b (P19a): the stall guard against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

The simulator keeps "slewing / tracking" at a mechanical limit (P14), which is exactly a stall.
Reproducing it through the gateway needs motion the gateway does not itself refuse or cut short:
- P14's below-horizon goto is stopped by the gateway's Lim EL monitor long before -40 deg, so the
  stall is made with the test's OWN pwi4_client (after sim_guard in the shared `pwi` fixture) using
  a large position OFFSET while tracking (found at T3.3b): axis 1 climbs to its +91 deg limit, or
  axis 0 sweeps to its +300 deg limit, and then sits there with a huge target distance.
- The gateway is started with the Sun pinned far below the horizon (PW_TEST_SUN_ALTAZ); the base
  altitude is chosen >= 30 deg from the real Sun's altitude so PWI4's own Sun handling never
  matters, whatever the time of day.

The mount ends stopped, not tracking, above its limits, offsets reset. Throwaway gateway only.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest

from gateway.tests.test_control_pwi4sim import (  # noqa: F401  (pwi is a fixture)
    ORIGIN,
    PORT,
    RESULTS,
    Client,
    Sampler,
    pwi,
    run,
)
from gateway.tests.test_sequences_pwi4sim import start_gateway
from gateway.tests.test_track_pwi4sim import (
    SUN_FAR,
    ephem,
    site_of,
    status,
    wait_arrived,
)

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

STALLED = "Mount stalled; home it first"
WARNING = (
    "Mount stalled (axis {n} not moving); it may be at a physical limit. "
    "Home the mount before further use."
)
MAX_LATENCY_S = 4.5  # 2.5 s stall window + 10 Hz poll / monitor + stop


class StallClient(Client):
    """Client that also keeps the snapshot's state_reason."""

    reason: str | None = None

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["type"] == "status":
                    d = msg["data"]
                    self.holder = d.get("control", {}).get("holder", "?")
                    self.homed = d.get("homed")
                    self.reason = d.get("state_reason")
                    if d["state"] != self.state:
                        self.state_log.append((time.monotonic(), d["state"]))
                    self.state = d["state"]
                elif msg["type"] == "ack":
                    self.acks[msg["id"]] = msg
                    ev = self._ack_events.get(msg["id"])
                    if ev:
                        ev.set()
        except Exception:
            pass

    async def wait_until(self, pred, timeout: float, what: str) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timeout waiting for {what}: state={self.state} homed={self.homed}")


def freeze_time(samples, idx: int) -> float | None:
    """Monotonic time from which axis `idx` (1 = axis0, 2 = axis1 in Sampler tuples) stays within
    1 arcsec of its final position to the end of the recording."""
    final = samples[-1][idx]
    last_moving = None
    for s in samples:
        if abs(s[idx] - final) * 3600.0 > 1.0:
            last_moving = s[0]
    return last_moving


@pytest.mark.parametrize("axis", [0, 1])
def test_gateway_stops_a_mount_stalled_at_its_mechanical_limit(pwi, tmp_path, axis):
    s0 = pwi.status()
    sun_alt, sun_az = ephem().sun_altaz(site_of(s0))
    base_alt = 20.0 if abs(20.0 - sun_alt) >= abs(72.0 - sun_alt) else 72.0
    # axis 1: the climb runs far from the Sun in azimuth. axis 0: axis 0 reads 150 deg inside a
    # wrap window [0, 360), so an offset of +180 deg puts the target at 330 > the +300 limit.
    base_az = (sun_az + 120.0) % 360.0 if axis == 1 else 150.0
    wrap0 = s0.mount.axis0_wrap_range_min_degs
    pwi.mount_stop()
    pwi.mount_tracking_off()
    if axis == 0:
        pwi.mount_set_axis0_wrap_range_min(0.0)  # stopped mount: nothing moves
    g = start_gateway(tmp_path, SUN_FAR)
    smp = None
    try:

        async def go():
            nonlocal smp

            async def to_pose(alt: float, az: float) -> None:
                """Vertical leg (axis 0: at an azimuth axis 0 can reach in the [0, 360) window,
                i.e. 0..300), then the azimuth sweep at that altitude (both clear of the real Sun
                by construction)."""
                az_now = (await status(pwi)).mount.azimuth_degs
                if axis == 0 and not 0.0 <= az_now < 300.0:
                    az_now = az
                for a, z in ((alt, az_now), (alt, az)):
                    pwi.mount_goto_alt_az(a, z)
                    await asyncio.sleep(1.0)
                    await wait_arrived(pwi)

            c = await StallClient.open(g.token())
            await c.wait_state("STOPPED", 8)
            await c.wait_until(
                lambda: c.homed and c.homed["state"] == "overridden", 5.0, "homed override"
            )

            # ---- the test's own client puts the mount in a known pose
            smp = Sampler(pwi).start()  # the whole run is recorded (diagnostics)
            t_start = time.monotonic()
            await to_pose(base_alt, base_az)
            # and creates the stall: tracking + a position offset far beyond the mechanical limit
            pwi.mount_tracking_on()
            await asyncio.sleep(0.5)
            assert c.homed["reason"] is None, (
                "stalled before the test's own stall",
                [
                    (round(x[0] - t_start, 1), round(x[1], 2), round(x[2], 2), x[3], x[4])
                    for x in smp.samples[:: max(1, len(smp.samples) // 40)]
                ],
            )
            if axis == 0:
                sign = 1  # axis 0 reads ~150 (see above)
                pwi.mount_offset(axis0_add_arcsec=sign * 180 * 3600)
            else:
                # push ~9 deg past the axis-1 upper limit from wherever the base pose is (a fixed
                # +40 deg from the 20 deg base, picked by the real Sun, never reached the limit)
                top = (await status(pwi)).mount.axis1.max_mech_position_degs or 91.0
                pwi.mount_offset(axis1_add_arcsec=(top + 9.0 - base_alt) * 3600)
            t_cmd = time.monotonic()

            await c.wait_until(
                lambda: c.homed and c.homed.get("reason") == "stalled", 60.0, "stalled guard"
            )
            t_seen = time.monotonic()
            await asyncio.sleep(1.0)
            smp.close()
            t_freeze = freeze_time(smp.samples, 1 + axis)
            assert t_freeze is not None
            latency = t_seen - t_freeze
            m = (await status(pwi)).mount
            ax = (m.axis0, m.axis1)[axis]
            res = {
                "axis": axis,
                "frozen_at_deg": round(ax.position_degs, 3),
                "target_distance_arcsec": round(ax.dist_to_target_arcsec),
                "time_to_reach_limit_s": round(t_freeze - t_cmd, 1),
                "stall_to_guard_s": round(latency, 2),
                "is_tracking_after": bool(m.is_tracking),
                "is_slewing_after": bool(m.is_slewing),
            }
            # one sample per second (axis 0, axis 1, tracking): diagnostics for a failed run
            res["trace"] = [
                (round(x[0] - t_cmd, 1), round(x[1], 2), round(x[2], 2), x[4])
                for x in smp.samples[:: max(1, len(smp.samples) // 40)]
                if x[0] >= t_cmd
            ]
            RESULTS[f"stall_axis{axis}"] = res
            print(f"\nstall axis {axis}:", res)
            assert latency <= MAX_LATENCY_S, res
            assert not m.is_tracking and not m.is_slewing, res
            await c.wait_state("STOPPED", 5)
            await c.wait_until(lambda: c.reason == WARNING.format(n=axis), 5.0, "stall warning")
            body = httpx.get(
                f"http://127.0.0.1:{PORT}/api/mount/status",
                headers={"x-mount-token": g.token(), "origin": ORIGIN},
                timeout=3,
            ).json()
            assert body["homed"]["reason"] == "stalled" and body["homed"]["state"] == "not_homed"

            # ---- refusals until a completed home
            for name, args in [
                ("goto_altaz", {"alt": 50.0, "az": base_az}),
                ("goto_radec_j2000", {"ra_h": 3.0, "dec_deg": 10.0}),
                ("tracking", {"on": True}),
                ("homed_override", None),
            ]:
                ack = await c.cmd(name, args)
                assert not ack["ok"] and ack["reason"] == STALLED, (name, ack)

            # ---- recover the sim with the test's own client: offsets off, back above the limit
            pwi.mount_offset(axis0_reset=1, axis1_reset=1)
            pwi.mount_tracking_off()
            await to_pose(base_alt, base_az)
            m = (await status(pwi)).mount
            assert not m.is_tracking and not m.is_slewing
            assert 0.0 < m.axis1.position_degs < 90.0, m.axis1.position_degs
            assert (
                c.homed["reason"] == "stalled"
            )  # moving the mount behind its back changes nothing

            # ---- only a completed home clears the guard
            ack = await c.cmd("home")
            assert ack["ok"], ack
            await c.wait_until(
                lambda: c.homed and c.homed["state"] == "homed" and c.homed["reason"] is None,
                30.0,
                "homed",
            )
            await c.wait_state("STOPPED", 8)
            assert c.reason is None
            await c.close()

        run(go())
    finally:
        if smp is not None:
            smp.close()
        g.kill()
        try:
            pwi.mount_stop()
            pwi.mount_tracking_off()
            pwi.mount_offset(axis0_reset=1, axis1_reset=1)
            if wrap0 is not None:
                pwi.mount_set_axis0_wrap_range_min(wrap0)
        except Exception:
            pass
    ev = g.audit_events()
    stalls = [e for e in ev if e["event"] == "stall_stop"]
    assert len(stalls) == 1, stalls
    st = stalls[0]
    print(
        f"\nstall_stop audit (axis {axis}):",
        {k: st[k] for k in ("axis", "dist_arcsec", "duration_s")},
    )
    assert st["axis"] == axis and st["delivered"] is True
    assert st["dist_arcsec"] > 900.0 and st["duration_s"] >= 2.5
    assert not [e for e in ev if e["event"] == "limit_stop"], "the Lim EL monitor fired"
    assert [e["how"] for e in ev if e["event"] == "homed"] == ["no_motion"]
