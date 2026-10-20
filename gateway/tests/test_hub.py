import asyncio

from gateway.hub import Hub


def test_drop_old_for_slow_client():
    async def go():
        hub = Hub()
        slow, fast = hub.add(), hub.add()
        got = []
        for i in range(5):
            hub.publish(f"m{i}")
            got.append(fast.queue.get_nowait())  # fast client keeps up
        return slow.queue.get_nowait(), slow.queue.qsize(), slow.dropped, got

    last, size, dropped, got = asyncio.run(go())
    assert last == "m4" and size == 0 and dropped == 4
    assert got == [f"m{i}" for i in range(5)]


def test_new_client_gets_latest_and_hooks_fire():
    async def go():
        hub = Hub()
        events = []
        hub.on_connect.append(lambda c: events.append("c"))
        hub.on_disconnect.append(lambda c: events.append("d"))
        hub.publish("x")
        c = hub.add()
        msg = c.queue.get_nowait()
        hub.remove(c)
        hub.remove(c)  # idempotent
        return msg, events

    msg, events = asyncio.run(go())
    assert msg == "x" and events == ["c", "d"]
