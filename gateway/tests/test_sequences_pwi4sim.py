# ruff: noqa: F811
"""T3.2 gate measurements against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

Reuses the throwaway-gateway / WebSocket helpers of the T3.1a sim tests (own port, temp settings
and audit dir). `sim_guard.assert_simulator()` runs in the shared `pwi` fixture before any PWI4
command. Motion is never commanded here: the sequences connect / enable / disconnect the sim mount
and HOME (a no-op on the simulator, P12); the Sun-blocked flows are refusals / skips. PWI4.cfg is
only READ (the `pwi` fixture compares its mtime and SHA-256 before / after). The PWI4 start step
is NOT exercised here (PWI4 must not be started or stopped by tests; it is unit-tested with a fake
launcher).
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from gateway.safety import AxisParams, axis0_target, predict_paths, sun_sep

from .test_control_pwi4sim import (  # noqa: F401  (pwi is a fixture)
    RESULTS,
    Client,
    Gateway,
    pwi,
    run,
)

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

HOME = (85.0, 180.0)  # (alt, az): the gateway's default home pose


# ------------------------------------------------------------------ helpers


class SeqClient(Client):
    """Client that also records the snapshot's `sequence` and every (state, step) pair seen."""

    sequence: dict | None = None
    seen: set[tuple[str, str | None]]
    reasons: list[str]

    async def _read(self) -> None:
        self.seen = set()
        self.reasons = []
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["type"] == "status":
                    d = msg["data"]
                    st = d["state"]
                    self.holder = d.get("control", {}).get("holder", "?")
                    self.sequence = d.get("sequence")
                    self.homed = d.get("homed")
                    step = self.sequence["step"] if self.sequence else None
                    self.seen.add((st, step))
                    if d.get("state_reason") and st in ("BUSY", "HOMING"):
                        if not self.reasons or self.reasons[-1] != d["state_reason"]:
                            self.reasons.append(d["state_reason"])
                    if st != self.state:
                        self.state_log.append((time.monotonic(), st))
                    self.state = st
                elif msg["type"] == "ack":
                    self.acks[msg["id"]] = msg
                    ev = self._ack_events.get(msg["id"])
                    if ev:
                        ev.set()
        except Exception:
            pass

    def states(self) -> set[str]:
        return {s for s, _ in self.seen}

    async def wait_sequence(self, timeout: float, name: str | None = None) -> dict:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            seq = self.sequence
            if seq and seq["status"] != "running" and (name is None or seq["name"] == name):
                return seq
            await asyncio.sleep(0.05)
        raise AssertionError(f"sequence did not end: {self.sequence}")

    async def wait_step(self, step: str, timeout: float) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if (
                self.sequence
                and self.sequence["step"] == step
                and self.sequence["status"] == "running"
            ):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"never reached step {step}: {self.sequence}")


def disconnect_sim(pwi) -> None:
    """Test infrastructure: put the sim in the 'disconnected' state (direct PWI4 client)."""
    pwi.mount_stop()
    pwi.mount_tracking_off()
    time.sleep(0.3)
    pwi.mount_disable(0)
    pwi.mount_disable(1)
    pwi.mount_disconnect()
    time.sleep(0.3)


def connect_sim(pwi) -> None:
    pwi.mount_connect()
    pwi.mount_enable(0)
    pwi.mount_enable(1)
    time.sleep(0.5)


def sim_state(pwi) -> dict:
    m = pwi.status().mount
    return {
        "connected": bool(m.is_connected),
        "enabled": (bool(m.axis0.is_enabled), bool(m.axis1.is_enabled)),
        "pos": (m.axis0.position_degs, m.axis1.position_degs),
    }


def start_gateway(tmp_path, sun: str = "-60,0") -> Gateway:
    g = Gateway(tmp_path)
    g.extra_env = {"PW_TEST_SUN_ALTAZ": sun}
    g.start()
    return g


def home_path_midpoint(status) -> tuple[float, float, float]:
    """(sun_alt, sun_az, start_sun_sep) placing the pinned Sun on the middle of the predicted
    path from the mount's current pose to the home pose (the no-acceleration-limit variant)."""
    m = status.mount
    a0, a1 = m.axis0, m.axis1
    tgt0 = axis0_target(
        a0.position_degs, HOME[1], a0.min_mech_position_degs, a0.max_mech_position_degs
    )
    p0 = AxisParams(a0.max_velocity_degs_per_sec or 20.0, a0.acceleration_degs_per_sec_sqr or 20.0)
    p1 = AxisParams(a1.max_velocity_degs_per_sec or 20.0, a1.acceleration_degs_per_sec_sqr or 20.0)
    paths = predict_paths((a0.position_degs, a1.position_degs), (tgt0, HOME[0]), p0, p1)
    path = paths[1]
    start = path[0]

    def clearance(q: tuple[float, float]) -> float:
        """Sky separation from this path sample to the start pose and to the home pose (min)."""
        here = (q[1], q[0] % 360.0)
        return min(sun_sep(start[0] % 360.0, start[1], here), sun_sep(HOME[1], HOME[0], here))

    # the Sun goes where the path is farthest (on the sky) from both ends: it is then on the
    # path, but neither the start pose nor the home pose is inside its disc
    mid = max(path, key=clearance)
    sun = (mid[1], mid[0] % 360.0)
    return sun[0], sun[1], sun_sep(start[0] % 360.0, start[1], sun)


# ------------------------------------------------------------------ tests


def test_connect_disconnect_connect_sequences(pwi, tmp_path):
    """connect -> connected, enabled, STOPPED; disconnect -> disconnected, axes disabled;
    connect again. BUSY and the step labels are seen in the status stream."""
    disconnect_sim(pwi)
    g = start_gateway(tmp_path)
    try:

        async def go():
            c = await SeqClient.open(g.token())
            await c.wait_state("DISCONNECTED", 5)
            out: dict = {}

            ack = await c.cmd("connect")
            assert ack["ok"], ack
            seq = await c.wait_sequence(40, "connect")
            assert seq["status"] == "done", seq
            await c.wait_state("STOPPED", 5)
            out["connect"] = {
                "steps_seen": sorted({s for _, s in c.seen if s}),
                "reasons": list(c.reasons),
            }
            out["after_connect"] = sim_state(pwi)
            assert out["after_connect"]["connected"] and out["after_connect"]["enabled"] == (
                True,
                True,
            )
            assert "BUSY" in c.states()
            # (the sim answers connect / enable instantly, faster than the 10 Hz stream samples)
            assert "home mount" in {s for _, s in c.seen if s}

            c.seen.clear()
            c.reasons.clear()
            ack = await c.cmd("disconnect")
            assert ack["ok"], ack
            seq = await c.wait_sequence(40, "disconnect")
            assert seq["status"] == "done", seq
            await c.wait_state("DISCONNECTED", 5)
            out["disconnect"] = {
                "steps_seen": sorted({s for _, s in c.seen if s}),
                "reasons": list(c.reasons),
            }
            out["after_disconnect"] = sim_state(pwi)
            assert not out["after_disconnect"]["connected"]
            assert "BUSY" in c.states()

            ack = await c.cmd("connect")
            assert ack["ok"], ack
            seq = await c.wait_sequence(40, "connect")
            assert seq["status"] == "done", seq
            await c.wait_state("STOPPED", 5)
            out["after_reconnect"] = sim_state(pwi)
            assert out["after_reconnect"]["connected"] and out["after_reconnect"]["enabled"] == (
                True,
                True,
            )
            RESULTS["sequences"] = out
            await c.close()

        run(go())
        ev = g.audit_events()
        fin = [e for e in ev if e["event"] == "home_finished"]
        assert len(fin) == 3 and all(e["how"] == "no_motion" for e in fin), fin  # P12
        assert not [e for e in ev if e["event"] in ("sequence_failed", "limit_stop")]
        assert not [e for e in ev if e["event"] == "pwi4_launch"]  # PWI4 was running
    finally:
        g.kill()


def test_home_on_the_sim_completes(pwi, tmp_path):
    connect_sim(pwi)
    g = start_gateway(tmp_path)
    try:

        async def go():
            c = await SeqClient.open(g.token())
            await c.wait_state("STOPPED", 5)
            before = sim_state(pwi)["pos"]
            ack = await c.cmd("home")
            assert ack["ok"], ack
            seq = await c.wait_sequence(30, "home")
            assert seq["status"] == "done", seq
            await c.wait_state("STOPPED", 5)
            assert "HOMING" in c.states()
            after = sim_state(pwi)["pos"]
            RESULTS["home"] = {"states": sorted(c.states()), "before": before, "after": after}
            await c.close()

        run(go())
        fin = [e for e in g.audit_events() if e["event"] == "home_finished"]
        assert fin and fin[0]["how"] == "no_motion"
    finally:
        g.kill()


def test_stop_mid_sequence_cancels_it(pwi, tmp_path):
    disconnect_sim(pwi)
    g = start_gateway(tmp_path)
    try:

        async def go():
            c = await SeqClient.open(g.token())
            await c.wait_state("DISCONNECTED", 5)
            assert (await c.cmd("connect"))["ok"]
            await c.wait_step("home mount", 30)  # the 3 s no-motion wait of the sim's find_home
            t0 = time.monotonic()
            ack = await c.cmd("stop")
            assert ack["ok"], ack
            seq = await c.wait_sequence(10)
            dt = time.monotonic() - t0
            assert seq["status"] == "cancelled" and seq["error"] == "Cancelled by STOP", seq
            await c.wait_state("STOPPED", 5)  # connected + enabled, BUSY cleared
            RESULTS["stop_mid_sequence"] = {"cancel_s": round(dt, 3), "state": c.state}
            assert sim_state(pwi)["connected"]
            await c.close()

        run(go())
        assert any(e["event"] == "sequence_cancelled" for e in g.audit_events())
    finally:
        g.kill()


def test_sun_blocked_home_flows(pwi, tmp_path):
    """Pinned test Sun on the predicted home path: connect skips home with a warning, a home
    command is refused, disconnect asks once and then disconnects without homing."""
    disconnect_sim(pwi)
    connect_sim(pwi)
    st = pwi.status()
    sun_alt, sun_az, start_sep = home_path_midpoint(st)
    pose0 = sim_state(pwi)["pos"]
    disconnect_sim(pwi)
    if start_sep < 20.0 or sun_alt < 5.0:
        pytest.skip(f"sim start pose unsuitable for a Sun-on-path test (sep {start_sep:.1f})")
    g = start_gateway(tmp_path, f"{sun_alt:.3f},{sun_az:.3f}")
    try:

        async def go():
            c = await SeqClient.open(g.token())
            await c.wait_state("DISCONNECTED", 5)
            out: dict = {"sun": (round(sun_alt, 2), round(sun_az, 2)), "pose0": pose0}

            assert (await c.cmd("connect"))["ok"]
            seq = await c.wait_sequence(40, "connect")
            await c.wait_state("STOPPED", 5)
            out["connect_warning"] = seq["warning"]
            assert seq["status"] == "done" and seq["error"] is None, seq
            assert seq["warning"] == "Home skipped: path passes within 15° of the Sun", seq
            s = sim_state(pwi)
            assert s["connected"] and s["enabled"] == (True, True)
            pos_after_connect = s["pos"]

            ack = await c.cmd("home")
            out["home_refusal"] = ack["reason"]
            assert not ack["ok"] and ack["reason"] == "Slew path passes within 15° of the Sun", ack

            ack = await c.cmd("disconnect")
            out["disconnect_first"] = {
                "ok": ack["ok"],
                "code": ack.get("code"),
                "reason": ack["reason"],
            }
            assert not ack["ok"] and ack["code"] == "home_blocked_confirm"
            assert ack["reason"] == "Home is blocked by the Sun; disconnect without homing?"
            assert sim_state(pwi)["connected"]  # "No": nothing changed

            ack = await c.cmd("disconnect", {"skip_home": True})
            assert ack["ok"], ack
            seq = await c.wait_sequence(40, "disconnect")
            assert seq["status"] == "done", seq
            await c.wait_state("DISCONNECTED", 5)
            assert not sim_state(pwi)["connected"]
            out["disconnect_warning"] = seq["warning"]
            RESULTS["sun_blocked"] = out
            assert max(abs(a - b) for a, b in zip(pose0, pos_after_connect, strict=True)) < 0.5
            await c.close()

        run(go())
        ev = [e["event"] for e in g.audit_events()]
        assert "home_skipped" in ev and "disconnect_without_home" in ev
        assert "home_finished" not in ev  # find_home was never sent
        assert "limit_stop" not in ev
    finally:
        g.kill()


def test_audit_has_the_sequence_trail(pwi, tmp_path):
    disconnect_sim(pwi)
    g = start_gateway(tmp_path)
    try:

        async def go():
            c = await SeqClient.open(g.token())
            await c.wait_state("DISCONNECTED", 5)
            assert (await c.cmd("connect"))["ok"]
            await c.wait_sequence(40, "connect")
            assert (await c.cmd("disconnect"))["ok"]
            await c.wait_sequence(40, "disconnect")
            await c.close()

        run(go())
        ev = g.audit_events()
        names = [e["event"] for e in ev]
        assert names.count("sequence_start") == 2 and names.count("sequence_done") == 2
        assert names.count("sequence_step") == 8
        cmds = [e for e in ev if e["event"] == "cmd" and e["cmd"] in ("connect", "disconnect")]
        assert [e["result"] for e in cmds] == ["ok", "ok"]
        assert "lock_taken" in names
    finally:
        g.kill()
