"""Gateway poller against PWI4 in simulation mode. Skipped unless PWI4_SIM=1.

The gateway (Poller) only GETs /status. The goto / STOP are driven from THIS TEST
through pwi4_client, after sim_guard.assert_simulator().
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "pwi4_harness"))
sys.path.insert(0, str(ROOT / "pwi4"))

from gateway.config import Settings  # noqa: E402
from gateway.ephem import Ephem  # noqa: E402
from gateway.hub import Hub  # noqa: E402
from gateway.poller import Poller  # noqa: E402

pytestmark = [
    pytest.mark.pwi4sim,
    pytest.mark.skipif(os.environ.get("PWI4_SIM") != "1", reason="set PWI4_SIM=1 to run"),
]

RESULTS: dict[str, object] = {}


@pytest.fixture(scope="module")
def pwi():
    import sim_guard
    from pwi4_client import PWI4

    sim_guard.assert_simulator()
    p = PWI4(sim_guard.HOST, sim_guard.PORT)
    p.mount_connect()
    p.mount_enable(0)
    p.mount_enable(1)
    yield p
    try:
        p.mount_stop()
        p.mount_disconnect()
    except Exception:
        pass
    print("\nPWI4SIM RESULTS:", RESULTS)


def make_poller() -> Poller:
    s = Settings()
    return Poller(s, Hub(), Ephem(s.ephemeris))


async def pump(poller: Poller, seconds: float, out: list, until=None) -> None:
    """Poll at 10 Hz for `seconds`, appending (t_mono, snapshot)."""
    end = time.monotonic() + seconds
    nxt = time.monotonic()
    while time.monotonic() < end:
        snap = await poller.poll_once()
        out.append((time.monotonic(), snap))
        if until is not None and until(snap):
            return
        nxt += 0.1
        await asyncio.sleep(max(0.0, nxt - time.monotonic()))


def test_snapshot_rate_at_least_9_5_hz(pwi):
    async def go():
        p = make_poller()
        out: list = []
        t0 = time.monotonic()
        # Run the real poll loop (poller.run) for 10 s, counting published messages.
        counter = {"n": 0}
        orig = p.hub.publish

        def counting(msg: str) -> None:
            counter["n"] += 1
            orig(msg)

        p.hub.publish = counting  # type: ignore[method-assign]
        task = asyncio.create_task(p.run())
        await asyncio.sleep(10.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return counter["n"], time.monotonic() - t0, p.latest, out

    n, dt, latest, _ = asyncio.run(go())
    rate = n / dt
    RESULTS["snapshot_rate_hz"] = round(rate, 2)
    assert latest is not None and latest.pwi4.reachable
    assert rate >= 9.5, rate


def test_goto_stop_state_sequence(pwi):
    async def go():
        p = make_poller()
        seen: list = []
        # settle at a known start pose
        await asyncio.to_thread(pwi.mount_goto_alt_az, 45.0, 180.0)
        await pump(p, 40, seen, until=lambda s: s.state == "STOPPED" and len(seen) > 20)
        await pump(p, 1.0, seen)
        assert seen[-1][1].state == "STOPPED"

        log: list = []
        await asyncio.to_thread(pwi.mount_goto_alt_az, 45.0, 100.0)
        await pump(p, 5.0, log, until=lambda s: s.state == "SLEWING")
        assert log[-1][1].state == "SLEWING"
        await pump(p, 1.5, log)
        t_stop = time.monotonic()
        await asyncio.to_thread(pwi.mount_stop)
        t_ack = time.monotonic()
        await pump(p, 5.0, log, until=lambda s: s.state == "STOPPED")
        return log, t_stop, t_ack

    log, t_stop, t_ack = asyncio.run(go())
    states = []
    for _, s in log:
        if not states or states[-1] != s.state:
            states.append(s.state)
    assert states == ["STOPPED", "SLEWING", "STOPPED"] or states == ["SLEWING", "STOPPED"], states

    def vmax(s):
        return max(abs(s.axes[0].velocity_deg_s or 0), abs(s.axes[1].velocity_deg_s or 0))

    last_moving_t = max(t for t, s in log if vmax(s) > 0.05)
    first_stopped_t = next(t for t, s in log if s.state == "STOPPED" and t > last_moving_t)
    after_motion = first_stopped_t - last_moving_t
    after_cmd = first_stopped_t - t_stop
    raw_slewing_at_stopped = next(
        s.mount.is_slewing_raw for t, s in log if s.state == "STOPPED" and t > last_moving_t
    )
    RESULTS.update(
        stopped_after_last_motion_s=round(after_motion, 3),
        stopped_after_stop_cmd_s=round(after_cmd, 3),
        stop_cmd_http_ms=round((t_ack - t_stop) * 1000, 1),
        is_slewing_raw_when_gateway_said_stopped=raw_slewing_at_stopped,
        states=states,
    )
    assert after_motion <= 0.5, after_motion
