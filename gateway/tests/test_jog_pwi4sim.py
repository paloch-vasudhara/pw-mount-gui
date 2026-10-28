# ruff: noqa: F811
"""T3.4c: JOG and STEP against PWI4 in SIMULATOR mode. Skipped unless PWI4_SIM=1.

Throwaway gateway subprocess on the shared test port; the gateway's Sun is pinned far below the
horizon (PW_TEST_SUN_ALTAZ) so the gateway's Sun checks never depend on the time of day. The REAL
Sun still matters to PWI4's own handling, so every pose is >= 100 deg of azimuth from the real Sun
and the sweeps are a few degrees. `sim_guard.assert_simulator()` runs in the shared `pwi` fixture
before any PWI4 command. Direct PWI4 calls here (the start pose, wrap window, offset reset) go
through the test's own pwi4_client, never through the gateway. PWI4.cfg is only READ.

Measured (printed + RESULTS): jog rate accuracy, release latency, keep-alive-loss stop time, step
accuracy (stopped and tracking; a tracking step ends TRACKING), a jog from TRACKING ending STOPPED,
the margin above Lim EL of a 5 deg/s jog toward it, and the wrap-out-of-range refusal. The mount
ends stopped, not tracking, offsets reset.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time

import pytest

from gateway.control import R_AZ_LIMIT
from gateway.tests.test_control_pwi4sim import (  # noqa: F401  (pwi is a fixture)
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
    radec_sep_arcsec,
    site_of,
    status,
    wait_arrived,
    wait_stationary,
)

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

LIM_EL = 15.0
RATE_TOL = 0.05  # +-5 % of the commanded axis rate
KA_LOSS_LIMIT_S = 0.5  # keep-alive loss -> physically stopped and not tracking


class JogClient(Client):
    """Client that also keeps the snapshot's jog / step fields and can send keep-alives."""

    jog: dict | None = None
    step: dict | None = None
    jog_limit: dict | None = None
    turn: dict | None = None
    _ka: asyncio.Task | None = None
    last_ka_sent = 0.0

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["type"] == "status":
                    d = msg["data"]
                    self.holder = d.get("control", {}).get("holder", "?")
                    self.homed = d.get("homed")
                    self.jog, self.step = d.get("jog"), d.get("step")
                    self.jog_limit, self.turn = d.get("jog_limit"), d.get("turn")
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

    def keepalive(self, args: dict) -> None:
        """Re-send the jog every 100 ms (like the web client) until `stop_keepalive`."""

        async def loop() -> None:
            n = 0
            while True:
                n += 1
                try:
                    msg = {"type": "cmd", "id": f"ka{n}", "cmd": "jog", "args": args}
                    await self.ws.send(json.dumps(msg))
                    self.last_ka_sent = time.monotonic()
                except Exception:
                    return
                await asyncio.sleep(0.1)

        self._ka = asyncio.create_task(loop())

    async def stop_keepalive(self) -> None:
        if self._ka is not None:
            self._ka.cancel()
            await asyncio.gather(self._ka, return_exceptions=True)
            self._ka = None

    async def until(self, pred, timeout: float, what: str) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return time.monotonic() - t0
            await asyncio.sleep(0.02)
        raise AssertionError(f"timeout waiting for {what}: state={self.state} jog={self.jog}")


# ------------------------------------------------------------------ infrastructure


def centred_axis0(sun_az: float) -> float:
    """An axis-0 position (-180..180) whose azimuth is 150 deg from the real Sun."""
    return ((sun_az + 150.0 + 180.0) % 360.0) - 180.0


async def put_pose(pwi, alt: float, x0: float) -> None:
    """Test infrastructure: stopped mount at alt / axis-0 position x0, wrap window centred on it."""
    pwi.mount_stop()
    pwi.mount_tracking_off()
    pwi.mount_offset(axis0_reset=1, axis1_reset=1)
    await wait_stationary(pwi)
    pwi.mount_set_axis0_wrap_range_min(x0 - 180.0)
    pwi.mount_goto_alt_az(alt, x0 % 360.0)
    await asyncio.sleep(1.0)
    await wait_arrived(pwi)


def fit_rate(samples, idx: int, t_from: float, t_to: float) -> float:
    seg = [s for s in samples if t_from <= s[0] <= t_to]
    assert len(seg) > 8, len(seg)
    n = len(seg)
    tm = sum(s[0] for s in seg) / n
    xm = sum(s[idx] for s in seg) / n
    num = sum((s[0] - tm) * (s[idx] - xm) for s in seg)
    den = sum((s[0] - tm) ** 2 for s in seg)
    return num / den


def settle_time(smp: Sampler, after: float) -> float | None:
    """Seconds from `after` until the mount is physically stationary AND not tracking."""
    t = smp.stopped_at(after, tracking_counts=True)
    return None if t is None else t - after


@pytest.fixture()
def setup(pwi, tmp_path):
    s0 = pwi.status()
    _, sun_az = ephem().sun_altaz(site_of(s0))
    x0 = centred_axis0(sun_az)
    g = start_gateway(tmp_path, SUN_FAR)
    try:
        yield pwi, g, x0
    finally:
        g.kill()
        try:
            pwi.mount_stop()
            pwi.mount_tracking_off()
            pwi.mount_offset(axis0_reset=1, axis1_reset=1)
        except Exception:
            pass


async def open_client(g) -> JogClient:
    c = await JogClient.open(g.token())
    await c.wait_state("STOPPED", 8)
    await c.until(lambda: c.homed and c.homed["state"] == "overridden", 5.0, "homed override")
    return c


# ------------------------------------------------------------------ tests


def test_jog_from_stopped_rate_release_and_keepalive_loss(setup):
    pwi, g, x0 = setup
    res: dict = {}

    async def go():
        await put_pose(pwi, 50.0, x0)
        c = await open_client(g)
        args = {"axis": 0, "dir": 1, "speed": 2.0}

        # ---- 3 s at 2 deg/s, released with jog_stop
        smp = Sampler(pwi).start()
        ack = await c.cmd("jog", args)
        assert ack["ok"], ack
        t_ack = time.monotonic()
        c.keepalive(args)
        await c.until(lambda: c.jog is not None, 2.0, "snapshot jog")
        assert c.jog == {"axis": 0, "dir": 1, "speed": 2.0, "origin": "stopped"}
        await asyncio.sleep(3.0 - (time.monotonic() - t_ack))
        await c.stop_keepalive()
        t_rel = time.monotonic()
        ack = await c.cmd("jog_stop")
        assert ack["ok"], ack
        res["jog_stop_ack_ms"] = round((time.monotonic() - t_rel) * 1000.0)
        await asyncio.sleep(1.2)
        smp.close()
        rate = fit_rate(smp.samples, 1, t_ack + 0.5, t_rel)
        lat = settle_time(smp, t_rel)
        m = (await status(pwi)).mount
        res.update(
            rate_deg_s=round(rate, 4),
            rate_err_pct=round((rate / 2.0 - 1.0) * 100.0, 2),
            release_to_stopped_s=None if lat is None else round(lat, 3),
            tracking_after=bool(m.is_tracking),
            travelled_deg=round(m.axis0.position_degs - x0, 3),
        )
        print("\nJOG 2 deg/s x 3 s from STOPPED:", res)
        assert abs(rate - 2.0) <= 2.0 * RATE_TOL, res
        assert lat is not None and lat <= 1.0, res
        assert not m.is_tracking, res
        await c.until(lambda: c.state == "STOPPED" and c.jog is None, 4.0, "STOPPED, jog null")

        # ---- keep-alive loss
        await wait_stationary(pwi)
        smp = Sampler(pwi).start()
        ack = await c.cmd("jog", {"axis": 0, "dir": -1, "speed": 2.0})
        assert ack["ok"], ack
        args = {"axis": 0, "dir": -1, "speed": 2.0}
        c.keepalive(args)
        await asyncio.sleep(1.0)
        await c.stop_keepalive()
        t_last = c.last_ka_sent
        await asyncio.sleep(1.5)
        smp.close()
        lat = settle_time(smp, t_last)
        m = (await status(pwi)).mount
        res["keepalive_loss_to_stopped_s"] = None if lat is None else round(lat, 3)
        res["tracking_after_loss"] = bool(m.is_tracking)
        print("JOG keep-alive loss:", res["keepalive_loss_to_stopped_s"], "s")
        assert lat is not None and lat <= KA_LOSS_LIMIT_S, res
        assert not m.is_tracking
        await c.until(lambda: c.state == "STOPPED" and c.jog is None, 4.0, "STOPPED after loss")
        await c.close()
        RESULTS["jog_stopped"] = res

    run(go())
    ev = g.audit_events()
    reasons = [e["reason"] for e in ev if e["event"] == "jog_stop"]
    assert reasons == ["release", "keepalive_timeout"], reasons
    assert len([e for e in ev if e["event"] == "jog_start"]) == 2
    print(
        "jog_stop audit:",
        [(e["reason"], e.get("release_ms"), e["held_s"]) for e in ev if e["event"] == "jog_stop"],
    )


def test_jog_from_tracking_ends_stopped_not_tracking(setup):
    pwi, g, x0 = setup
    res: dict = {}

    async def go():
        await put_pose(pwi, 50.0, x0)
        c = await open_client(g)
        assert (await c.cmd("tracking", {"on": True}))["ok"]
        await c.wait_state("TRACKING", 8)
        await asyncio.sleep(1.0)
        pwi.mount_offset(axis0_reset=1, axis1_reset=1)  # clean baseline (test infrastructure)
        m0 = (await status(pwi)).mount
        args = {"axis": 0, "dir": 1, "speed": 1.0}
        smp = Sampler(pwi).start()
        ack = await c.cmd("jog", args)
        assert ack["ok"], ack
        c.keepalive(args)
        await c.until(lambda: c.jog is not None, 2.0, "snapshot jog")
        assert c.jog["origin"] == "tracking"
        await asyncio.sleep(2.0)
        await c.stop_keepalive()
        t_rel = time.monotonic()
        assert (await c.cmd("jog_stop"))["ok"]
        await asyncio.sleep(1.5)
        smp.close()
        lat = settle_time(smp, t_rel)
        m1 = (await status(pwi)).mount
        res.update(
            moved_deg=round(m1.axis0.position_degs - m0.axis0.position_degs, 3),
            release_to_stopped_s=None if lat is None else round(lat, 3),
            is_tracking=bool(m1.is_tracking),
        )
        print("\nJOG from TRACKING 1 deg/s x 2 s:", res)
        RESULTS["jog_tracking"] = res
        assert not m1.is_tracking, res
        assert lat is not None and lat <= 1.0, res
        await c.until(lambda: c.state == "STOPPED" and c.jog is None, 4.0, "STOPPED, jog null")
        await c.close()

    run(go())
    ev = g.audit_events()
    stops = [e for e in ev if e["event"] == "jog_stop"]
    assert [(e["reason"], e["origin"]) for e in stops] == [("release", "tracking")]


@pytest.mark.parametrize("origin", ["stopped", "tracking"])
def test_step_10_arcmin_accuracy(setup, origin):
    pwi, g, x0 = setup
    res: dict = {"origin": origin}

    async def go():
        await put_pose(pwi, 50.0, x0)
        c = await open_client(g)
        if origin == "tracking":
            assert (await c.cmd("tracking", {"on": True}))["ok"]
            await c.wait_state("TRACKING", 8)
            await asyncio.sleep(1.0)
        for axis, dir_ in ((1, 1), (0, -1)):
            m0 = (await status(pwi)).mount
            ack = await c.cmd("step", {"axis": axis, "dir": dir_, "size_arcmin": 10})
            assert ack["ok"], ack
            await c.until(lambda: c.step is not None, 2.0, "snapshot step")
            assert c.step == {"axis": axis, "dir": dir_, "size_arcmin": 10}
            second = await c.cmd("step", {"axis": axis, "dir": dir_, "size_arcmin": 10})
            assert not second["ok"] and second["reason"] == "Step in progress", second
            took = await c.until(lambda: c.step is None, 15.0, "step done")
            await asyncio.sleep(0.6)
            m1 = (await status(pwi)).mount
            if origin == "stopped":
                a0, a1 = (m0.axis0, m0.axis1)[axis], (m1.axis0, m1.axis1)[axis]
                delta = (a1.position_degs - a0.position_degs) * 3600.0
                err = abs(delta - dir_ * 600.0)
            else:  # tracking: the sky moves; the J2000 RA/Dec separation is the step
                delta = radec_sep_arcsec(
                    m0.ra_j2000_hours, m0.dec_j2000_degs, m1.ra_j2000_hours, m1.dec_j2000_degs
                )
                # azimuth steps are on-sky cos(alt) shorter
                want = 600.0 * (math.cos(math.radians(m0.altitude_degs)) if axis == 0 else 1.0)
                err = abs(delta - want)
            res[f"axis{axis}"] = {
                "delta_arcsec": round(delta, 1),
                "err_arcsec": round(err, 1),
                "done_s": round(took, 2),
            }
            assert err <= 25.0, res
            if origin == "stopped":
                assert not m1.is_tracking and c.state == "STOPPED", res
            else:  # ends TRACKING at the stepped position
                assert m1.is_tracking and c.state == "TRACKING", res
                res[f"axis{axis}"]["tracking_after"] = True
        print(f"\nSTEP 10' ({origin}):", res)
        RESULTS[f"step_{origin}"] = res
        await c.cmd("stop")
        await wait_stationary(pwi)
        await c.close()

    run(go())
    ev = g.audit_events()
    assert len([e for e in ev if e["event"] == "step"]) == 2
    assert all(e["origin"] == origin for e in ev if e["event"] == "step")
    if origin == "stopped":
        assert not [e for e in ev if e["event"] == "cmd" and e.get("cmd") == "tracking"]
    else:  # a step from TRACKING ends TRACKING: still tracking, nothing had to be resumed
        ends = [e for e in ev if e["event"] == "step_end"]
        assert len(ends) == 2 and all(e.get("tracking_resumed") is False for e in ends), ends


def test_jog_toward_lim_el_stops_above_it(setup):
    pwi, g, x0 = setup
    res: dict = {}

    async def go():
        await put_pose(pwi, 32.0, x0)
        c = await open_client(g)
        args = {"axis": 1, "dir": -1, "speed": 5.0}
        smp = Sampler(pwi).start()
        ack = await c.cmd("jog", args)
        assert ack["ok"], ack
        c.keepalive(args)
        t0 = time.monotonic()
        await c.until(lambda: c.jog is not None, 2.0, "snapshot jog")
        await c.until(lambda: c.jog is None, 12.0, "jog ended by a limit")
        res["jog_ended_after_s"] = round(time.monotonic() - t0, 2)
        await c.stop_keepalive()
        last = [a for k, a in c.acks.items() if k.startswith("ka")][-3:]
        print("last keep-alive acks:", [(a["ok"], a["reason"]) for a in last])
        await asyncio.sleep(1.5)
        smp.close()
        m = (await status(pwi)).mount
        lowest = min(s[2] for s in smp.samples)
        res.update(
            lowest_alt_deg=round(lowest, 3),
            final_alt_deg=round(m.altitude_degs, 3),
            margin_above_lim_el_deg=round(lowest - LIM_EL, 3),
            tracking_after=bool(m.is_tracking),
        )
        print("\nJOG 5 deg/s toward Lim EL (15):", res)
        RESULTS["jog_lim_el"] = res
        assert lowest >= LIM_EL, res
        assert not m.is_tracking, res
        await c.until(lambda: c.state == "STOPPED", 4.0, "STOPPED")
        await c.close()

    run(go())
    ev = g.audit_events()
    stops = [e for e in ev if e["event"] == "jog_stop"]
    assert [e["reason"] for e in stops] == ["limit"], stops
    print(
        "limit end:",
        {k: (stops[0].get(k)) for k in ("reason", "held_s")},
        "limit_stop events:",
        len([e for e in ev if e["event"] == "limit_stop"]),
    )


def test_jog_outside_the_wrap_window_recentres_it_and_does_not_turn(setup):
    """T3.4e option B: the gateway re-centres PWI4's wrap window on the axis before tracking_on,
    so the jog starts in place (no 360 deg normalising turn, P19)."""
    pwi, g, x0 = setup

    async def go():
        await put_pose(pwi, 50.0, x0)
        pwi.mount_set_axis0_wrap_range_min(x0 + 30.0)  # stopped mount: nothing moves
        await asyncio.sleep(0.5)
        c = await open_client(g)
        smp = Sampler(pwi).start()
        ack = await c.cmd("jog", {"axis": 0, "dir": 1, "speed": 2.0})
        for _ in range(15):
            await asyncio.sleep(0.1)
            await c.cmd("jog", {"axis": 0, "dir": 1, "speed": 2.0})
        await c.cmd("jog_stop", {})
        await asyncio.sleep(1.0)
        smp.close()
        x = [s[1] for s in smp.samples]
        steps = [abs(b - a) for a, b in zip(x, x[1:], strict=False)]
        moved, peak = x[-1] - x[0], max(steps)
        print("JOG outside the wrap window:", ack["ok"], "moved %.2f, max %.3f" % (moved, peak))
        assert ack["ok"], ack
        assert peak < 1.0  # no normalising swing (a 360 turn runs at ~20 deg/s)
        assert 1.5 < moved < 5.0  # ~2 deg/s for ~1.6 s, forward
        await c.close()
        pwi.mount_set_axis0_wrap_range_min(x0 - 180.0)

    run(go())
    ev = g.audit_events()
    assert [e for e in ev if e["event"] == "wrap_recentre"]


# ------------------------------------------------------------------ T3.4e azimuth wrap handling


@pytest.fixture()
def setup_sun_path(pwi, tmp_path):
    """Like `setup`, but the gateway's Sun is pinned ON the 360 deg sweep of the turn (alt 50,
    az 150), far from the pose itself."""
    s0 = pwi.status()
    g = start_gateway(tmp_path, "50,150")
    try:
        yield pwi, g, s0
    finally:
        g.kill()
        try:
            pwi.mount_stop()
            pwi.mount_tracking_off()
            pwi.mount_offset(axis0_reset=1, axis1_reset=1)
            pwi.mount_set_axis0_wrap_range_min(-180.0)
        except Exception:
            pass


def max_rate(samples, t_from: float, t_to: float, idx: int = 1, win: float = 0.1) -> float:
    """Largest |d position / dt| (deg/s) over any stretch of at least `win` seconds."""
    seg = [x for x in samples if t_from <= x[0] <= t_to]
    best, j = 0.0, 0
    for i, a in enumerate(seg):
        j = max(j, i + 1)
        while j < len(seg) and seg[j][0] - a[0] < win:
            j += 1
        if j >= len(seg):
            break
        best = max(best, abs(seg[j][idx] - a[idx]) / (seg[j][0] - a[0]))
    return best


def test_az_jog_slides_the_wrap_window_across_the_old_edge(setup):
    pwi, g, x0 = setup
    res: dict = {}

    async def go():
        await put_pose(pwi, 50.0, x0)
        pwi.mount_set_axis0_wrap_range_min(x0 - 335.0)  # the old window edge: 25 deg ahead
        await asyncio.sleep(0.5)
        c = await open_client(g)
        args = {"axis": 0, "dir": 1, "speed": 10.0}
        smp = Sampler(pwi).start()
        t0 = time.monotonic()
        ack = await c.cmd("jog", args)
        assert ack["ok"], ack
        c.keepalive(args)
        await asyncio.sleep(5.5)  # crosses the old edge (x0 + 25) at ~2.5 s
        await c.stop_keepalive()
        t_rel = time.monotonic()
        assert (await c.cmd("jog_stop"))["ok"]
        await asyncio.sleep(1.5)
        smp.close()
        m = (await status(pwi)).mount
        mx = max_rate(smp.samples, t0, t_rel + 0.3)
        res.update(
            x0=round(x0, 2),
            travelled_deg=round(m.axis0.position_degs - x0, 2),
            max_rate_deg_s_over_0p1s=round(mx, 2),
            samples=len(smp.samples),
            wrap_min_before=round(x0 - 335.0, 2),
            wrap_min_after=round(m.axis0_wrap_range_min_degs, 2),
            tracking_after=bool(m.is_tracking),
        )
        print("\nAZ JOG 10 deg/s across the old wrap edge:", res)
        RESULTS["az_slide"] = res
        assert mx <= 14.0, res  # a 360 deg swing would show 30+ deg/s
        assert 40.0 <= res["travelled_deg"] <= 70.0, res  # about 10 deg/s * 5.5 s, forward only
        assert res["wrap_min_after"] > x0 - 335.0 + 20.0, res  # the window moved with the axis
        assert not m.is_tracking, res
        await c.until(lambda: c.state == "STOPPED" and c.jog is None, 4.0, "STOPPED, jog null")
        await c.close()
        pwi.mount_set_axis0_wrap_range_min(-180.0)

    run(go())
    ev = g.audit_events()
    slides = [e for e in ev if e["event"] == "wrap_slide"]
    print("wrap_slide audit:", [(e["pos"], e["old_min"], e["new_min"]) for e in slides])
    assert slides and slides[0]["dir"] == 1
    assert [e["reason"] for e in ev if e["event"] == "jog_stop"] == ["release"]


def test_az_jog_stops_before_the_mechanical_limit_then_wrap_turn(setup):
    pwi, g, _ = setup
    res: dict = {}
    start_x = 255.0

    async def go():
        await put_pose(pwi, 50.0, start_x)
        c = await open_client(g)
        m0 = (await status(pwi)).mount
        lim = m0.axis0.max_mech_position_degs
        args = {"axis": 0, "dir": 1, "speed": 10.0}
        smp = Sampler(pwi).start()
        ack = await c.cmd("jog", args)
        assert ack["ok"], ack
        c.keepalive(args)
        t0 = time.monotonic()
        await c.until(lambda: c.jog is None and c.jog_limit is not None, 12.0, "limit stop")
        res["jog_ended_after_s"] = round(time.monotonic() - t0, 2)
        stop_state = c.jog_limit
        await c.stop_keepalive()
        await asyncio.sleep(1.5)
        smp.close()
        m1 = (await status(pwi)).mount
        final = m1.axis0.position_degs
        res.update(
            mech_limit=lim,
            final_axis0=round(final, 3),
            margin_to_limit_deg=round(lim - final, 3),
            expected_stop_margin_deg=9.0,
            jog_limit=stop_state,
            tracking_after=bool(m1.is_tracking),
        )
        print("\nAZ JOG 10 deg/s to the mechanical limit:", res)
        assert lim - final >= 2.0, res  # never inside the 2 deg beyond the stopping distance
        assert lim - final <= 9.0 + 4.0, res
        assert not m1.is_tracking and c.state == "STOPPED", res
        assert stop_state["axis"] == 0 and stop_state["dir"] == 1, res
        assert stop_state["limit_deg"] == lim and stop_state["turn_deg"] == 360.0, res
        assert stop_state["est_s"] > 0, res

        # a jog that way is refused and prompts again
        ack = await c.cmd("jog", args)
        assert not ack["ok"] and ack["reason"] == R_AZ_LIMIT and ack["code"] == "az_limit", ack
        assert c.jog is None

        # ---- the turn: same az/el, the other representation of axis 0
        alt0, az0 = m1.altitude_degs, m1.azimuth_degs
        smp = Sampler(pwi).start()
        t_turn = time.monotonic()
        ack = await c.cmd("wrap_turn")
        assert ack["ok"], ack
        await c.until(lambda: c.turn is not None, 3.0, "snapshot turn")
        turn = c.turn
        assert c.jog_limit is None
        took = await c.until(lambda: c.turn is None, 90.0, "turn done")
        await asyncio.sleep(1.0)
        smp.close()
        m2 = (await status(pwi)).mount
        daz = ((m2.azimuth_degs - az0 + 180.0) % 360.0 - 180.0) * 3600.0
        dalt = (m2.altitude_degs - alt0) * 3600.0
        res["turn"] = {
            "from_deg": round(final, 3),
            "to_deg_snapshot": turn["to_deg"],
            "est_s": stop_state["est_s"],
            "final_axis0": round(m2.axis0.position_degs, 3),
            "axis0_error_arcsec": round((m2.axis0.position_degs - (final - 360.0)) * 3600.0, 1),
            "az_error_arcsec": round(daz, 1),
            "alt_error_arcsec": round(dalt, 1),
            "duration_s": round(took, 1),
            "tracking_after": bool(m2.is_tracking),
            "max_axis0_rate": round(max_rate(smp.samples, t_turn, t_turn + took + 1, 1), 1),
        }
        print("AZ WRAP TURN:", res["turn"])
        RESULTS["az_limit_turn"] = res
        assert abs(m2.axis0.position_degs - (final - 360.0)) < 0.05, res
        assert abs(daz) < 60.0 and abs(dalt) < 60.0, res
        assert not m2.is_tracking, res
        await c.until(lambda: c.state == "STOPPED", 4.0, "STOPPED after the turn")
        assert c.jog_limit is None
        await c.close()
        pwi.mount_set_axis0_wrap_range_min(-180.0)

    run(go())
    ev = g.audit_events()
    stop = [e for e in ev if e["event"] == "jog_stop"]
    assert [e["reason"] for e in stop] == ["mech_limit"], stop
    print("jog_stop audit:", {k: stop[0].get(k) for k in ("reason", "held_s", "release_ms")})
    assert [e["how"] for e in ev if e["event"] == "jog_limit"] == ["jog_end"]
    wt = [e for e in ev if e["event"] == "wrap_turn"]
    end = [e for e in ev if e["event"] == "wrap_turn_end"]
    print("wrap_turn audit:", wt, end)
    assert len(wt) == 1 and len(end) == 1 and end[0]["how"] == "done"
    assert not [e for e in ev if e["event"] in ("stall_stop", "limit_stop")], ev


def test_wrap_turn_stop_and_stay(setup):
    pwi, g, _ = setup

    async def go():
        await put_pose(pwi, 50.0, 255.0)
        c = await open_client(g)
        args = {"axis": 0, "dir": 1, "speed": 10.0}
        assert (await c.cmd("jog", args))["ok"]
        c.keepalive(args)
        await c.until(lambda: c.jog is None and c.jog_limit is not None, 12.0, "limit stop")
        await c.stop_keepalive()
        await asyncio.sleep(1.0)
        # stay: the prompt goes, the mount does not move
        before = (await status(pwi)).mount.axis0.position_degs
        assert (await c.cmd("wrap_turn_cancel"))["ok"]
        await c.until(lambda: c.jog_limit is None, 2.0, "prompt cleared")
        await asyncio.sleep(0.5)
        assert abs((await status(pwi)).mount.axis0.position_degs - before) < 0.01
        # a turn, then STOP half-way
        assert (await c.cmd("wrap_turn"))["ok"]
        await c.until(lambda: c.turn is not None, 3.0, "snapshot turn")
        await asyncio.sleep(2.0)
        assert (await c.cmd("stop"))["ok"]
        await c.until(lambda: c.turn is None, 3.0, "turn cancelled")
        await wait_stationary(pwi)
        m = (await status(pwi)).mount
        print("\nTURN stopped by STOP at axis0:", round(m.axis0.position_degs, 2), "from", before)
        assert before - 360.0 + 5.0 < m.axis0.position_degs < before - 5.0
        assert not m.is_tracking
        await c.close()
        pwi.mount_set_axis0_wrap_range_min(-180.0)

    run(go())
    ev = g.audit_events()
    assert [e["how"] for e in ev if e["event"] == "wrap_turn_end"] == ["stop"]
    assert len([e for e in ev if e["event"] == "wrap_turn_cancel"]) == 1


def test_wrap_turn_with_the_sun_on_its_path_is_routed(setup_sun_path):
    """T3.4e refused this turn; since T3.5 (D-SUN) it is routed around the Sun. The full routed
    turn is covered by test_dsun_pwi4sim; here: accepted as a detour, and STOP ends it."""
    pwi, g, _ = setup_sun_path

    async def go():
        await put_pose(pwi, 50.0, 255.0)
        c = await open_client(g)
        ack = await c.cmd("wrap_turn")
        print("\nTURN with the Sun on its path:", ack)
        assert ack["ok"], ack
        await asyncio.sleep(4.0)
        await c.cmd("stop")
        await asyncio.sleep(1.5)
        await c.close()
        pwi.mount_set_axis0_wrap_range_min(-180.0)

    run(go())
    ev = g.audit_events()
    det = [e for e in ev if e["event"] == "sun_detour"]
    assert det and det[0]["kind"] == "wrap_turn", det
    assert [e for e in ev if e["event"] == "sun_detour_stopped"]
    assert not [e for e in ev if e["event"] == "sun_detour_done"]
