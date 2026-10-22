"""Publisher must keep its cadence regardless of how PWI4 / the fetch behaves."""

import asyncio
import json
import time

from gateway.config import Settings
from gateway.ephem import Ephem
from gateway.hub import Hub
from gateway.poller import Poller

from .conftest import FakePwi4


def make(fake: FakePwi4) -> tuple[Poller, list]:
    s = Settings()
    p = Poller(s, Hub(), Ephem(s.ephemeris), fake.transport)
    got: list = []
    orig = p.hub.publish

    def rec(msg: str) -> None:
        got.append((time.monotonic(), json.loads(msg)["data"]))
        orig(msg)

    p.hub.publish = rec  # type: ignore[method-assign]
    return p, got


def test_hanging_status_does_not_slow_publisher_and_goes_stale():
    async def go():
        fake = FakePwi4()
        p, got = make(fake)
        task = asyncio.create_task(p.run())
        await asyncio.sleep(0.6)  # healthy
        fake.delay = 2.0  # /status now hangs
        t_hang = time.monotonic()
        await asyncio.sleep(3.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return got, t_hang

    got, t_hang = asyncio.run(go())
    window = [(t, d) for t, d in got if t >= t_hang]
    rate = len(window) / 3.0
    assert rate >= 9.0, rate
    assert got[0][1]["state"] in ("STOPPED", "DISCONNECTED")
    states = [(t - t_hang, d["state"], d["state_reason"]) for t, d in window]
    # fresh for the first ~1 s, then "not responding"
    assert any(s == "STOPPED" for dt, s, _r in states if dt < 0.8)
    late = [(s, r) for t, s, r in states if t > 1.3]
    assert late and all(x == ("DISCONNECTED", "PWI4 not responding") for x in late)
    assert window[-1][1]["pwi4"]["age_ms"] > 1000
    assert [d["seq"] for _, d in got] == sorted(d["seq"] for _, d in got)


def test_connect_timeout_closed_port_keeps_rate_and_reason():
    async def go():
        fake = FakePwi4()
        fake.connect_timeout_after = 0.5  # what a closed localhost port does on Windows
        p, got = make(fake)
        task = asyncio.create_task(p.run())
        await asyncio.sleep(3.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return got, fake

    got, fake = asyncio.run(go())
    assert len(got) / 3.0 >= 9.0
    assert all(d["state"] == "DISCONNECTED" for _, d in got)
    assert got[-1][1]["state_reason"] == "PWI4 not running / not reachable"
    assert len(fake.paths) <= 4  # backed off, not hammering


def test_stale_gap_is_not_motion():
    from .conftest import status_text

    async def go():
        fake = FakePwi4()
        p, _ = make(fake)
        p.stale_s = 1.0
        await p.poll_once(now_mono=0.0)
        fake.text = status_text({"mount.axis0.position_degs": 90.0, "mount.is_slewing": True})
        stale = p.publish_once(now_mono=2.0)  # fetch outstanding, last good 2 s old
        await p.fetch_once(now_mono=2.1)  # big position jump after the gap
        back = p.publish_once(now_mono=2.1)
        return stale, back

    stale, back = asyncio.run(go())
    assert stale.state == "DISCONNECTED" and stale.state_reason == "PWI4 not responding"
    assert back.state != "SLEWING"  # gap resets the tracker: first sample has v=0
    assert back.axes[0].velocity_deg_s == 0.0
