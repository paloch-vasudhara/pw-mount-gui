"""T5.4b ADS-B integration: aircraft from the SIP AERO platform (or the simulated 8500 feed).

The pipeline itself (platform client, CPR, times, heights, prediction) is the sibling repo's
`adsb_core` library (read only for us). This module owns the gateway side:

- an OPTIONAL import (`load_core`): missing / broken / not 0.3.x -> state `module_missing`, the
  gateway starts and every satellite / mount function works as before;
- two sources behind one interface: LIVE (`adsb_core.platform.PlatformClient` ->
  `AircraftTable`) and SIMULATED (a client of adsb_interpreter's feed on 8500, started with
  `--simulate`; each snapshot aircraft is adapted to an `adsb_core.state.Aircraft`, so the same
  `adsb_core.predict.path` cues both);
- the runner: its own asyncio tasks with bounded queues. A failing or slow source never blocks
  the PWI4 poller, the 10 Hz hub or a satellite track (events are only ever queued by the source
  callbacks; ingesting yields to the loop every few hundred events);
- the 1 Hz `aircraft` message on the hub's separate slot, and the `adsb` block for the status.

The read token comes from the secret store (`adsb_read_token`) only; it is handed to the platform
client and never logged, never put in a state detail, never sent to the web (J2).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import httpx

from .hub import Hub
from .settings import SettingsStore

log = logging.getLogger("gateway.adsb")

SUPPORTED_PREFIX = "0.3."
SITE_HAE_OFFSET_M = -30.0  # HAE = MSL - 30 m (the geoid near Bangkok; adsb_interpreter's rule)
DEFAULT_FEED_URL = "http://127.0.0.1:8500"
TICK_S = 1.0
EVENT_QUEUE_MAX = 2000  # live events waiting for the table (oldest dropped beyond that)
SNAPSHOT_QUEUE_MAX = 4  # simulated feed snapshots waiting to be adapted
DRAIN_BATCH = 200  # events ingested before yielding to the loop
STALE_AFTER_S = 10.0  # position older: faded (Q42)
HIDE_AFTER_S = 60.0  # position older: not shown (Q42)
FEED_HELLO_TIMEOUT_S = 10.0

LIVE_GREEN = frozenset({"connected", "stale"})
SIMULATED_GREEN = frozenset({"simulated"})

DETAIL_TEXT = {
    "disabled": "ADS-B is switched off in Settings",
    "not_configured": "Set the ADS-B URL and read token in Settings",
    "connecting": "Connecting to the ADS-B platform",
    "connected": "Connected to the ADS-B platform",
    "stale": "Connected, no data",
    "unreachable": "ADS-B platform unreachable",
    "auth_failed": "ADS-B token rejected (check the read token)",
    "tls_error": "ADS-B TLS verification failed",
    "error": "ADS-B connection error",
    "simulated": "Simulated aircraft (adsb_interpreter feed 8500)",
}
SIM_DETAIL_TEXT = {
    "connecting": "Connecting to the simulated feed (adsb_interpreter 8500)",
    "unreachable": "Simulated feed unreachable (adsb_interpreter 8500)",
    "auth_failed": "Simulated feed refused the session (adsb_interpreter 8500)",
    "error": "Simulated feed error (adsb_interpreter 8500)",
}


def is_green(state: str, simulated: bool) -> bool:
    """The Q62 enable rule: live = connected / stale; simulated = the feed reports `simulated`."""
    return state in (SIMULATED_GREEN if simulated else LIVE_GREEN)


def simulated_from_env() -> bool:
    return os.environ.get("PW_ADSB_SIMULATE") == "1"


def feed_url_from_env() -> str:
    return os.environ.get("PW_ADSB_FEED_URL") or DEFAULT_FEED_URL


# ---------------------------------------------------------------- optional import


@dataclass(frozen=True)
class Core:
    version: str
    platform: ModuleType
    state: ModuleType
    predict: ModuleType
    geometry: ModuleType
    timebase: ModuleType


def load_core() -> tuple[Core | None, str]:
    """(core, "") or (None, why). Never raises: a missing / broken / other-version adsb_core only
    disables the aircraft feature."""
    try:
        import adsb_core

        version = str(getattr(adsb_core, "__version__", ""))
        if not version.startswith(SUPPORTED_PREFIX):
            return None, f"ADS-B module version {version or '?'} is not supported (need 0.3.x)"
        mods = [
            importlib.import_module(f"adsb_core.{n}")
            for n in ("platform", "state", "predict", "geometry", "timebase")
        ]
        return Core(version, *mods), ""
    except ModuleNotFoundError as exc:
        if exc.name == "adsb_core":
            return None, "ADS-B module not installed"
        return None, "ADS-B module failed to load (ModuleNotFoundError)"
    except Exception as exc:
        return None, f"ADS-B module failed to load ({type(exc).__name__})"


# ---------------------------------------------------------------- the simulated-feed adapter


def adapt_feed_aircraft(core: Core, row: dict, t: float, prev: Any = None) -> Any | None:
    """One aircraft of an 8500 `aircraft` snapshot (feed/live.py) -> `adsb_core.state.Aircraft`.

    fix_t = t - pos_age_s; east / north from gs_kt + track_deg; velocity_t = fix_t. A row with no
    position yields an Aircraft without a fix (predict returns nothing for it). The snapshot's
    pos_age_s is rounded to 0.1 s: the fix time only advances when the position changes, so a
    repeated fix keeps one stable fix_t. `prev` (the same ICAO's earlier Aircraft) keeps the
    Kalman filter running for aircraft without a velocity.
    """
    icao = row.get("icao")
    if not isinstance(icao, str) or not icao:
        return None
    st = core.state
    ac = prev if prev is not None else st.Aircraft(icao=icao, first_seen=t, last_seen=t)
    seen = row.get("seen_age_s")
    ac.last_seen = t - (seen if isinstance(seen, int | float) else 0.0)
    ac.callsign = row.get("callsign") or None
    ac.category = row.get("category")
    lat, lon, age = row.get("lat"), row.get("lon"), row.get("pos_age_s")
    if lat is None or lon is None or age is None:
        ac.lat = ac.lon = ac.fix_t = None
        ac.height_m = None
        ac.kalman = None
        return ac
    fix_t = t - float(age)
    new_fix = ac.fix_t is None or ac.lat != lat or ac.lon != lon
    h = row.get("height_m")
    ac.baro_altitude_ft = row.get("alt_ft")
    ac.height_m = None if h is None else float(h)
    ac.height_source = row.get("height_source")
    ac.fix_qualified = bool(row.get("qualified"))
    if new_fix:
        ac.lat, ac.lon, ac.fix_t = float(lat), float(lon), fix_t
        h0 = ac.height_m if ac.height_m is not None else 0.0
        geo = core.geometry.Geodetic(ac.lat, ac.lon, h0)
        if ac.kalman is None:
            ac.kalman = core.predict.CvKalman(fix_t, geo, ac.fix_qualified)
        else:
            ac.kalman.update(fix_t, geo, ac.fix_qualified)
    gs, trk = row.get("gs_kt"), row.get("track_deg")
    if gs is not None and trk is not None:
        a = math.radians(float(trk))
        ac.ground_speed_kt, ac.track_deg = float(gs), float(trk)
        ac.east_kt, ac.north_kt = float(gs) * math.sin(a), float(gs) * math.cos(a)
        ac.velocity_t = ac.fix_t
    else:
        ac.ground_speed_kt = ac.track_deg = ac.east_kt = ac.north_kt = None
        ac.velocity_t = None
    ac.vertical_rate_fpm = row.get("vrate_fpm")
    ac.turn_rate_dps = row.get("turn_dps")
    return ac


# ---------------------------------------------------------------- sources

Report = Callable[..., None]  # report(state, detail="", last_event_at=None)


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret and len(secret) >= 6 else text


class LiveSource:
    """`PlatformClient` -> bounded queue -> `AircraftTable.ingest` (expired every 1 s)."""

    simulated = False

    def __init__(
        self,
        core: Core,
        url: str,
        token: str,
        site: Any,
        range_limit_km: float,
        report: Report,
        clock: Callable[[], float] = time.time,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.core = core
        self.table = core.state.AircraftTable(site, range_limit_km)
        self._url = url
        self._token = token
        self._report = report
        self._clock = clock
        self._factory = client_factory or self._default_factory
        self._q: asyncio.Queue[tuple[float, dict]] = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)
        self.dropped = 0
        self.errors = 0

    def _default_factory(self, url, token, on_event, on_status):
        return self.core.platform.PlatformClient(
            url, token, on_event, kinds=("decoded",), on_status=on_status
        )

    def _on_event(self, event: dict) -> None:
        """Called from the client's task: only queue (never block, never do real work)."""
        if self._q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._q.get_nowait()
                self.dropped += 1
        self._q.put_nowait((self._clock(), event))

    def _on_status(self, st: Any) -> None:
        try:
            state = str(getattr(st.state, "value", st.state))
            detail = _scrub(str(getattr(st, "detail", "") or ""), self._token)
            self._report(state, detail, getattr(st, "last_event_at", None))
        except Exception:
            self.errors += 1

    async def run(self) -> None:
        client = self._factory(self._url, self._token, self._on_event, self._on_status)
        tasks = [asyncio.create_task(client.run()), asyncio.create_task(self._drain())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _drain(self) -> None:
        while True:
            now, ev = await self._q.get()
            n = 0
            while True:
                try:
                    self.table.ingest(ev, now)
                except Exception:  # a bad message never stops the table
                    self.errors += 1
                n += 1
                if n % DRAIN_BATCH == 0:
                    await asyncio.sleep(0)
                try:
                    now, ev = self._q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await asyncio.sleep(0)

    # -- the source interface
    def aircraft(self) -> list[Any]:
        return list(self.table)

    def get(self, icao: str) -> Any | None:
        return self.table.get(icao)

    def expire(self, now: float) -> None:
        self.table.expire(now)

    def delay(self, now: float) -> tuple[float | None, float | None]:
        s = self.table.delays.summary(now)
        return s.p50_s, s.p95_s

    def counts(self) -> dict[str, int]:
        c = {k: int(v) for k, v in self.table.counts.items()}
        c["dropped"] = self.dropped
        if self.errors:
            c["errors"] = self.errors
        return c


class SimulatedSource:
    """A client of adsb_interpreter's feed (default http://127.0.0.1:8500, `--simulate`)."""

    simulated = True

    def __init__(
        self,
        core: Core,
        feed_url: str,
        report: Report,
        clock: Callable[[], float] = time.time,
        backoff_s: tuple[float, float] = (1.0, 10.0),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.core = core
        self._base = feed_url.rstrip("/")
        parts = urlsplit(self._base)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._ws_url = self._base.replace("http", "ws", 1) + "/api/feed/ws"
        self._report = report
        self._clock = clock
        self._backoff = backoff_s
        self._backoff_now = backoff_s[0]
        self._transport = transport
        self._aircraft: dict[str, Any] = {}
        self._q: asyncio.Queue[tuple[float, dict]] = asyncio.Queue(maxsize=SNAPSHOT_QUEUE_MAX)
        self._delays = core.timebase.DelayStats()
        self._snapshots = 0
        self.dropped = 0
        self.errors = 0

    # -- connection
    async def _session_token(self) -> str:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=5.0, trust_env=False
        ) as c:
            r = await c.get(
                f"{self._base}/api/feed/session", headers={"Sec-Fetch-Site": "none"}
            )
        if r.status_code != 200:
            raise _FeedError("auth_failed", f"HTTP {r.status_code}")
        token = r.json().get("token")
        if not isinstance(token, str) or not token:
            raise _FeedError("error", "no session token")
        return token

    async def _connect_once(self) -> None:
        from websockets.asyncio.client import connect

        token = await self._session_token()
        async with connect(
            self._ws_url,
            origin=self._origin,
            proxy=None,
            open_timeout=5,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 << 20,
        ) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            hello = json.loads(await asyncio.wait_for(ws.recv(), FEED_HELLO_TIMEOUT_S))
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                raise _FeedError("error", "unexpected first message")
            self._backoff_now = self._backoff[0]
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "aircraft":
                    if self._q.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self._q.get_nowait()
                            self.dropped += 1
                    self._q.put_nowait((self._clock(), msg))

    async def _connect_loop(self) -> None:
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        self._backoff_now = self._backoff[0]
        while True:
            self._report("connecting", "")
            state, detail = "unreachable", "connection closed"
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except _FeedError as exc:
                state, detail = exc.state, exc.detail
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                if code == 4401:
                    state, detail = "auth_failed", "session rejected (4401)"
            except InvalidHandshake as exc:
                state, detail = "auth_failed", type(exc).__name__
            except (OSError, TimeoutError, httpx.HTTPError) as exc:
                state, detail = "unreachable", type(exc).__name__
            except Exception as exc:
                state, detail = "error", type(exc).__name__
            self._report(state, detail)
            await asyncio.sleep(self._backoff_now)
            self._backoff_now = min(self._backoff_now * 2, self._backoff[1])

    async def _consume(self) -> None:
        while True:
            now, msg = await self._q.get()
            try:
                self._apply(now, msg)
            except Exception:
                self.errors += 1
            await asyncio.sleep(0)

    def _apply(self, now: float, msg: dict) -> None:
        feed = msg.get("feed") if isinstance(msg.get("feed"), dict) else {}
        fstate = str(feed.get("state") or "")
        if fstate != "simulated":
            self._aircraft.clear()
            self._report("error", f"the feed reports state {fstate or '?'}, not simulated")
            return
        t = msg.get("t")
        if not isinstance(t, int | float):
            return
        rows = msg.get("aircraft")
        seen: dict[str, Any] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            ac = adapt_feed_aircraft(self.core, row, float(t), self._aircraft.get(row.get("icao")))
            if ac is not None:
                seen[ac.icao] = ac
        self._aircraft = seen
        self._snapshots += 1
        self._delays.add(now, float(t))
        self._report("simulated", "", now)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._connect_loop()), asyncio.create_task(self._consume())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- the source interface
    def aircraft(self) -> list[Any]:
        return list(self._aircraft.values())

    def get(self, icao: str) -> Any | None:
        return self._aircraft.get(icao)

    def expire(self, now: float) -> None:
        pass  # the feed's snapshot is the whole truth: absent = gone

    def delay(self, now: float) -> tuple[float | None, float | None]:
        s = self._delays.summary(now)
        return s.p50_s, s.p95_s

    def counts(self) -> dict[str, int]:
        c = {"snapshots": self._snapshots, "dropped": self.dropped}
        if self.errors:
            c["errors"] = self.errors
        return c


class _FeedError(Exception):
    def __init__(self, state: str, detail: str) -> None:
        super().__init__(detail)
        self.state, self.detail = state, detail


# ---------------------------------------------------------------- rows for the web


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None else round(float(v), nd)


def aircraft_rows(
    core: Core, aircraft: list[Any], site: Any, range_limit_m: float, now: float
) -> list[dict]:
    """The visible aircraft: within the range limit, above 0 deg, a position <= 60 s old."""
    out = []
    for ac in aircraft:
        if ac.fix_t is None or ac.lat is None or ac.lon is None or ac.height_m is None:
            continue
        age = now - ac.fix_t
        if age > HIDE_AFTER_S:
            continue
        look = core.geometry.look_angles(
            site, core.geometry.Geodetic(ac.lat, ac.lon, ac.height_m)
        )
        if look.el < 0.0 or look.range > range_limit_m:
            continue
        pred = core.predict.position_at(ac, now)
        out.append(
            {
                "icao": ac.icao,
                "callsign": ac.callsign,
                "lat": _r(ac.lat, 5),
                "lon": _r(ac.lon, 5),
                "az": _r(look.az, 3),
                "el": _r(look.el, 3),
                "range_m": _r(look.range, 0),
                "track_deg": _r(ac.track_deg, 1),
                "alt_ft": _r(ac.baro_altitude_ft, 0),
                "gs_kt": _r(ac.ground_speed_kt, 1),
                "vrate_fpm": _r(ac.vertical_rate_fpm, 0),
                "pos_age_s": _r(max(age, 0.0), 1),
                "stale": age > STALE_AFTER_S,
                "height_source": None if ac.height_source is None else str(ac.height_source),
                "predictor": None if pred is None else pred.method,
            }
        )
    out.sort(key=lambda r: r["icao"])
    return out


# ---------------------------------------------------------------- the runner


class AdsbRunner:
    """Owns the source, its task and the 1 Hz tick. `apply()` (re)starts it from the Settings."""

    def __init__(
        self,
        store: SettingsStore,
        hub: Hub,
        *,
        simulated: bool | None = None,
        feed_url: str | None = None,
        client_factory: Callable[..., Any] | None = None,
        loader: Callable[[], tuple[Core | None, str]] = load_core,
        clock: Callable[[], float] = time.time,
        feed_backoff_s: tuple[float, float] = (1.0, 10.0),
        feed_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.hub = hub
        self.simulated = simulated_from_env() if simulated is None else simulated
        self.feed_url = feed_url or feed_url_from_env()
        self._client_factory = client_factory
        self._loader = loader
        self._clock = clock
        self._feed_backoff = feed_backoff_s
        self._feed_transport = feed_transport
        self._core: Core | None = None
        self._source: LiveSource | SimulatedSource | None = None
        self._task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._gen = 0
        self._seq = 0
        self._tick_rows = 0  # aircraft rows in the last tick
        self._sent_rows = False  # the last published frame had aircraft
        self._site: Any = None
        self._range_limit_m = 100_000.0
        self._range_limit_km = 100
        self._state = "disabled"
        self._raw_detail = ""
        self._last_event_at: float | None = None
        self._stats: dict[str, Any] = {"p50": None, "p95": None, "counts": {}}
        self._lock = asyncio.Lock()

    # -- lifecycle
    async def start(self) -> None:
        await self.apply()
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._tick_loop(), name="adsb-tick")

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
            self._tick_task = None
        await self._stop_source()

    async def _stop_source(self) -> None:
        self._gen += 1  # late reports of the old source are ignored
        task, self._task, self._source = self._task, None, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def apply(self) -> None:
        """(Re)start from the current Settings: a Settings save calls this."""
        async with self._lock:
            await self._stop_source()
            gen = self._gen
            cfg = self.store.current
            loc = cfg.location
            core, why = self._loader()
            self._core = core
            self._range_limit_km = cfg.adsb.range_limit_km
            self._range_limit_m = cfg.adsb.range_limit_km * 1000.0
            self._last_event_at = None
            self._stats = {"p50": None, "p95": None, "counts": {}}

            def report(state: str, detail: str = "", last_event_at: float | None = None) -> None:
                if gen == self._gen:
                    self._set(state, detail, last_event_at)

            if core is None:
                self._site = None
                self._set("module_missing", why)
                return self._publish_now()
            self._site = core.geometry.Geodetic(
                loc.lat_deg, loc.lon_deg, loc.alt_m + SITE_HAE_OFFSET_M
            )
            source: LiveSource | SimulatedSource | None = None
            if self.simulated:
                source = SimulatedSource(
                    core,
                    self.feed_url,
                    report,
                    self._clock,
                    self._feed_backoff,
                    self._feed_transport,
                )
                self._set("connecting", "")
            elif not cfg.adsb.enabled:
                self._set("disabled", "")
            else:
                try:
                    token = self.store.adsb_secrets.get()
                except Exception:
                    token = None
                if not cfg.adsb.url or not token:
                    self._set("not_configured", "")
                else:
                    source = LiveSource(
                        core,
                        cfg.adsb.url,
                        token,
                        self._site,
                        cfg.adsb.range_limit_km,
                        report,
                        self._clock,
                        self._client_factory,
                    )
                    self._set("connecting", "")
            if source is not None:
                self._source = source
                self._task = asyncio.create_task(self._run_source(source, gen), name="adsb-source")
            self._publish_now()

    async def _run_source(self, source: Any, gen: int) -> None:
        try:
            await source.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never include the message (it could carry a URL / token)
            if gen == self._gen:
                self._set("error", f"source failed ({type(exc).__name__})")
                log.warning("ADS-B source failed (%s)", type(exc).__name__)

    # -- state
    def _set(self, state: str, detail: str = "", last_event_at: float | None = None) -> None:
        if state != self._state:
            log.info("ADS-B state: %s", state)
        self._state, self._raw_detail = state, detail
        if last_event_at is not None:
            self._last_event_at = last_event_at

    @property
    def state(self) -> str:
        return self._state

    @property
    def green(self) -> bool:
        return self._core is not None and is_green(self._state, self.simulated)

    def detail(self) -> str:
        s, raw = self._state, self._raw_detail
        if s == "module_missing":
            return raw
        text = (SIM_DETAIL_TEXT.get(s) if self.simulated else None) or DETAIL_TEXT.get(s, s)
        if raw and s in ("stale", "unreachable", "auth_failed", "tls_error", "error", "connecting"):
            return f"{text}: {raw}"
        return text

    def block(self) -> dict:
        """The `adsb` block (status + aircraft message). Cheap: values from the last tick."""
        age = None
        if self._last_event_at is not None:
            age = round(max(0.0, self._clock() - self._last_event_at), 1)
        return {
            "state": self._state,
            "green": self.green,
            "simulated": self.simulated,
            "detail": self.detail(),
            "delay_p50_s": _r(self._stats["p50"], 2),
            "delay_p95_s": _r(self._stats["p95"], 2),
            "counts": dict(self._stats["counts"]),
            "last_event_age_s": age,
            "range_limit_km": self._range_limit_km,
        }

    @property
    def core(self) -> Core | None:
        """The loaded adsb_core (None = the module is missing): aircraft cueing needs it."""
        return self._core

    @property
    def range_limit_m(self) -> float:
        return self._range_limit_m

    def aircraft_for(self, icao: str) -> Any | None:
        """The `adsb_core.state.Aircraft` for an ICAO (both sources), for `predict.path`."""
        src = self._source
        return None if src is None else src.get(icao)

    # -- the 1 Hz message
    def tick(self, now: float | None = None) -> str:
        now = self._clock() if now is None else now
        rows: list[dict] = []
        src = self._source
        if src is not None and self._core is not None:
            src.expire(now)
            p50, p95 = src.delay(now)
            counts = src.counts()
            all_ac = src.aircraft()
            if self.green:
                rows = aircraft_rows(self._core, all_ac, self._site, self._range_limit_m, now)
            counts["aircraft"] = len(all_ac)
            counts["visible"] = len(rows)
            self._stats = {"p50": p50, "p95": p95, "counts": counts}
        self._tick_rows = len(rows)
        return self._message(rows)

    def _message(self, rows: list[dict]) -> str:
        self._seq += 1
        return json.dumps(
            {
                "type": "aircraft",
                "seq": self._seq,
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "data": {"adsb": self.block(), "aircraft": rows},
            },
            separators=(",", ":"),
        )

    def _publish_now(self) -> None:
        """Every second the stats are refreshed; the frame goes out only while there are aircraft,
        plus one empty frame when the last one goes (the web clears its list). No ADS-B = no
        frames: the 10 Hz status carries the `adsb` block anyway."""
        try:
            msg = self.tick()
            had, self._sent_rows = self._sent_rows, self._tick_rows > 0
            if self._tick_rows > 0 or had:
                self.hub.publish_aux(msg)
        except Exception as exc:
            log.warning("ADS-B publish failed (%s)", type(exc).__name__)

    async def _tick_loop(self) -> None:
        nxt = time.monotonic()
        while True:
            self._publish_now()
            nxt += TICK_S
            delay = nxt - time.monotonic()
            if delay < -TICK_S:
                nxt, delay = time.monotonic(), 0.0
            await asyncio.sleep(max(0.0, delay))
