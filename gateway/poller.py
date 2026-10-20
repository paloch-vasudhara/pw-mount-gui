"""PWI4 poller. Sends only GET /status.

Two independent asyncio tasks so a slow/absent PWI4 never delays the stream:
- fetch loop: polls /status at poll_hz while reachable, retries every RETRY_S otherwise;
- publish ticker: publishes the latest snapshot at poll_hz (new seq, fresh ts, state re-evaluated).
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import httpx

from .config import Settings
from .ephem import Ephem
from .hub import Hub
from .pwi4_io import PollResult, fetch_status, new_client
from .settings import SettingsStore, compute_site_check
from .snapshot import Limits, MountSnapshot, Site, SiteCheck, build_snapshot, site_key
from .state import MotionTracker

STALE_S = 1.0
RETRY_S = 1.0
REASON_NOT_RESPONDING = "PWI4 not responding"


def envelope(snapshot: MountSnapshot) -> str:
    return '{"type":"status","seq":%d,"ts":"%s","data":%s}' % (
        snapshot.seq,
        snapshot.ts_utc,
        snapshot.model_dump_json(),
    )


class Poller:
    def __init__(
        self,
        settings: Settings,
        hub: Hub,
        ephem: Ephem,
        transport: httpx.AsyncBaseTransport | None = None,
        store: SettingsStore | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.hub = hub
        self.ephem = ephem
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.tracker = MotionTracker()
        self.latest: MountSnapshot | None = None
        self.seq = 0
        self.retry_s = RETRY_S
        self.stale_s = STALE_S
        self.last_good: PollResult | None = None
        self.last_good_t = 0.0
        self.last_result: PollResult | None = None
        self.vel = (0.0, 0.0)

    async def fetch_once(self, now_mono: float | None = None) -> PollResult:
        if self._client is None:
            self._client = new_client(self.settings.pwi4_url, self._transport)
        result = await fetch_status(self._client)
        now = time.monotonic() if now_mono is None else now_mono
        self.last_result = result
        if result.reachable:
            self.last_good, self.last_good_t = result, now
            m = result.status.mount
            v0, v1, _ = self.tracker.update(now, m.axis0.position_degs, m.axis1.position_degs)
            self.vel = (v0, v1)
        elif result.kind == "unreachable":
            self.tracker.reset()
            self.vel = (0.0, 0.0)
        await self.refresh_ephem()
        return result

    def site_tuple(self) -> tuple[float, float, float] | None:
        """Site used for Sun/Moon/LMST: Settings location if a store is attached, else PWI4's."""
        if self.store is not None:
            loc = self.store.current.location
            return (loc.lat_deg, loc.lon_deg, loc.alt_m)
        return site_key(self.last_good) if self.last_good is not None else None

    async def refresh_ephem(self, force: bool = False) -> None:
        site = self.site_tuple()
        if site is not None and (force or self.ephem.stale(site)):
            with contextlib.suppress(Exception):  # Sun/Moon are advisory
                await asyncio.to_thread(self.ephem.update, site)

    def pwi4_site(self) -> tuple[float, float, float] | None:
        """PWI4's own site, only while PWI4 is live (fresh good fetch)."""
        good = self.last_good
        if good is None or time.monotonic() - self.last_good_t > self.stale_s:
            return None
        if self.last_result is None or self.last_result.kind == "unreachable":
            return None
        return site_key(good)

    def current_site_check(self) -> SiteCheck | None:
        if self.store is None:
            return None
        return SiteCheck(**compute_site_check(self.store.current.location, self.pwi4_site()))

    def publish_once(self, now_mono: float | None = None) -> MountSnapshot:
        now = time.monotonic() if now_mono is None else now_mono
        good, last = self.last_good, self.last_result
        down_reason = None
        if good is None or last is None or last.kind == "unreachable":
            result = last or PollResult(False, error="no data yet", kind="unreachable")
        elif now - self.last_good_t > self.stale_s:
            result, down_reason = good, REASON_NOT_RESPONDING
            self.tracker.reset()  # a stale gap must never read as motion
            self.vel = (0.0, 0.0)
        else:
            result = good
        live = result.reachable and down_reason is None
        site = site_check = limits = None
        if self.store is not None:
            cur = self.store.current
            site = Site(
                lat=cur.location.lat_deg,
                lon=cur.location.lon_deg,
                height_m=cur.location.alt_m,
                source="settings",
            )
            site_check = self.current_site_check()
            limits = Limits(lim_el_deg=cur.hardware.lim_el_deg)
        age_ms = None if good is None else max(0.0, (now - self.last_good_t) * 1000.0)
        self.seq += 1
        snap = build_snapshot(
            seq=self.seq,
            result=result,
            vel=self.vel,
            moving=self.tracker.moving_at(now) if live else False,
            sunmoon=self.ephem.cached if (good is not None or site is not None) else None,
            age_ms=age_ms,
            down_reason=down_reason,
            site=site,
            site_check=site_check,
            limits=limits,
        )
        self.latest = snap
        self.hub.publish(envelope(snap))
        return snap

    async def poll_once(self, now_mono: float | None = None) -> MountSnapshot:
        """Fetch then publish once (tests / simple callers)."""
        await self.fetch_once(now_mono)
        return self.publish_once(now_mono)

    async def _fetch_loop(self) -> None:
        period = 1.0 / self.settings.poll_hz
        nxt = time.monotonic()
        while True:
            t0 = time.monotonic()
            try:
                ok = (await self.fetch_once()).reachable
            except asyncio.CancelledError:
                raise
            except Exception:
                ok = False
            if ok:
                nxt += period
                delay = nxt - time.monotonic()
                if delay < -period:
                    nxt = time.monotonic()
                    delay = 0.0
            else:
                delay = self.retry_s - (time.monotonic() - t0)
                nxt = time.monotonic() + max(0.0, delay)
            await asyncio.sleep(max(0.0, delay))

    async def _publish_loop(self) -> None:
        period = 1.0 / self.settings.poll_hz
        nxt = time.monotonic()
        while True:
            try:
                self.publish_once()
            except Exception:
                pass
            nxt += period
            delay = nxt - time.monotonic()
            if delay < -period:
                nxt = time.monotonic()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._fetch_loop()), asyncio.create_task(self._publish_loop())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._client is not None:
                await self._client.aclose()
