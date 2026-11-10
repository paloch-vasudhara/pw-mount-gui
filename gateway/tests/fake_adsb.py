"""Test doubles for the ADS-B side (T5.4b): a fake `PlatformClient`, synthetic decoded envelopes
(adsb_core's own CPR encoder) and a fake adsb_interpreter feed (8500) server on a throwaway port.
Nothing here imports adsb_interpreter's feed package or touches a real server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from datetime import UTC, datetime
from http import HTTPStatus

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

# gateway/settings.py default location (Settings seed)
SITE_LAT, SITE_LON = 13.7862, 101.4639
THROWAWAY_PORTS = range(18750, 18760)
SESSION_TOKEN = "fake-feed-session-token"


# ---------------------------------------------------------------- the platform side


class FakeStatus:
    """What adsb_core.platform.Status carries, as far as the gateway reads it."""

    def __init__(self, state: str, detail: str = "", last_event_at: float | None = None) -> None:
        self.state = state
        self.detail = detail
        self.last_event_at = last_event_at


class FakeClient:
    """Stands in for PlatformClient: `script(client)` is an async function that drives it."""

    instances: list[FakeClient] = []

    def __init__(self, url, token, on_event, on_status, script=None) -> None:
        self.url, self.token, self.on_event, self.on_status = url, token, on_event, on_status
        self.script = script
        FakeClient.instances.append(self)

    def status(self, state: str, detail: str = "", last_event_at: float | None = None) -> None:
        self.on_status(FakeStatus(state, detail, last_event_at))

    async def run(self) -> None:
        self.status("connecting")
        if self.script is None:
            await asyncio.sleep(3600)
        else:
            await self.script(self)
        await asyncio.sleep(3600)  # a real client returns only on a fatal state; stay quiet


def factory(script=None):
    FakeClient.instances = []
    return lambda url, token, on_event, on_status: FakeClient(
        url, token, on_event, on_status, script
    )


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _envelope(icao: str, t: float, tc: int, decoded: dict, seq: int) -> dict:
    p = {
        "schema": "sip.aero.adsb.decoded.v1", "message_id": f"{icao}-{seq}",
        "source_event_id": str(seq), "device_id": "TEST", "source_kind": "live",
        "input_topic": "adsb/frames", "channel": 2, "received_at": _iso(t),
        "decoded_at": _iso(t + 0.03), "timestamp": _iso(t - 0.08), "timestamp_status": 0,
        "timestamp_source": 6, "df": 17, "ca": 5, "icao": icao, "type_code": tc, "raw": None,
        "frame_basis": "parsed_live", "crc_valid": None, "signal_raw": 40,
        "signal_unit": "receiver_raw_scale", "archive": None, "decoded": decoded,
        "upstream_adsb": None,
    }
    return {
        "id": p["source_event_id"], "received_at": p["received_at"], "source_at": None,
        "kind": "decoded", "device_id": "TEST", "topic": "adsb/decoded", "channel": 2,
        "icao": icao, "retained": False, "qos": 1, "payload": p,
    }


def overflight_events(
    icao: str = "ABC123", callsign: str | None = "TST100", t0: float | None = None,
    seconds: int = 12, north_km: float = 10.0, alt_ft: float = 35000.0, gs_kt: float = 450.0,
) -> list[dict]:
    """Decoded envelopes of an aircraft flying due east `north_km` north of the site, one
    position per second (even / odd), a velocity after each, an identification at the start."""
    from adsb_core import cpr
    from adsb_core.geometry import Geodetic, from_enu
    from adsb_core.predict import KT

    t0 = time.time() - seconds if t0 is None else t0
    out: list[dict] = []
    seq = 0
    for k in range(seconds):
        t = t0 + k
        p = from_enu(
            Geodetic(SITE_LAT, SITE_LON, 0.0), -3000.0 + gs_kt * KT * k, north_km * 1000.0, 0.0
        )
        odd = k % 2 == 1
        yz, xz = cpr.encode_airborne(p.lat, p.lon, odd)
        seq += 1
        out.append(_envelope(icao, t, 11, {
            "type": "airborne_position", "barometric_altitude_ft": alt_ft, "gnss_altitude_m": None,
            "cpr_format": "odd" if odd else "even", "cpr_latitude_code": yz,
            "cpr_longitude_code": xz, "latitude_deg": None, "longitude_deg": None,
            "position_status": "needs_CPR_pair_or_valid_reference"}, seq))
        seq += 1
        out.append(_envelope(icao, t + 0.5, 19, {
            "type": "airborne_velocity", "subtype": 1, "ground_speed_kt": gs_kt,
            "ground_track_deg": 90.0, "ground_track_reference": "true_north",
            "vertical_rate_fpm": 0, "vertical_rate_source": "barometric",
            "gnss_minus_baro_altitude_ft": 300, "nac_v_code": 2}, seq))
        if callsign and k == 0:
            seq += 1
            out.append(_envelope(icao, t + 0.2, 4, {
                "type": "identification", "callsign": callsign, "category": 3}, seq))
    return out


# ---------------------------------------------------------------- the fake 8500 feed


def feed_row(icao, lat, lon, height_m, *, t=None, callsign=None, gs=450.0, track=90.0,
             vrate=0.0, turn=None, pos_age=0.5, alt_ft=None, source="baro+diff") -> dict:
    return {
        "icao": icao, "callsign": callsign, "category": 3, "lat": lat, "lon": lon,
        "alt_ft": alt_ft if alt_ft is not None or height_m is None else round(height_m / 0.3048),
        "height_m": height_m, "height_source": source, "gs_kt": gs, "track_deg": track,
        "vrate_fpm": vrate, "turn_dps": turn, "sel_alt_ft": None, "qualified": False,
        "pos_age_s": pos_age, "seen_age_s": pos_age, "stale": (pos_age or 0) > 10,
        "visible": True, "az_deg": None, "el_deg": None, "range_m": None,
    }


def offset_row(icao, east_km, north_km, height_m, **kw) -> dict:
    from adsb_core.geometry import Geodetic, from_enu

    p = from_enu(Geodetic(SITE_LAT, SITE_LON, 0.0), east_km * 1000.0, north_km * 1000.0, 0.0)
    return feed_row(icao, round(p.lat, 5), round(p.lon, 5), height_m, **kw)


class FakeFeed:
    """adsb_interpreter's feed as the gateway sees it: GET /api/feed/session (needs
    Sec-Fetch-Site: none) and WS /api/feed/ws (Origin checked, auth first, hello, snapshots)."""

    def __init__(self, rows=None, state="simulated", interval=0.1) -> None:
        self.rows: list[dict] = rows if rows is not None else []
        self.state = state
        self.interval = interval
        self.port = 0
        self.session_calls: list[dict] = []
        self.auth_ok = 0
        self.auth_bad = 0
        self.connections = 0
        self.refuse_session = False
        self._server = None
        self._seq = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _process_request(self, connection, request):
        if request.path.split("?")[0] == "/api/feed/session":
            hdrs = dict(request.headers.items())
            self.session_calls.append({k.lower(): v for k, v in hdrs.items()})
            if self.refuse_session or request.headers.get("Sec-Fetch-Site") != "none":
                return connection.respond(HTTPStatus.FORBIDDEN, '{"detail":"forbidden"}')
            resp = connection.respond(HTTPStatus.OK, json.dumps({"token": SESSION_TOKEN}))
            resp.headers["Content-Type"] = "application/json"
            return resp
        if request.headers.get("Origin") != self.url:
            return connection.respond(HTTPStatus.FORBIDDEN, "bad origin")
        return None

    async def _handler(self, ws) -> None:
        self.connections += 1
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except Exception:
            return
        if not (msg.get("type") == "auth" and msg.get("token") == SESSION_TOKEN):
            self.auth_bad += 1
            await ws.close(4401)
            return
        self.auth_ok += 1
        try:
            await ws.send(json.dumps({"type": "hello", "service": "feed", "adsb_core": "0.3.0"}))
            while True:
                self._seq += 1
                await ws.send(json.dumps({
                    "type": "aircraft", "seq": self._seq, "t": round(time.time(), 3),
                    "feed": {"state": self.state, "last_event_age_s": 0.1}, "site": None,
                    "aircraft": self.rows,
                }))
                await asyncio.sleep(self.interval)
        except ConnectionClosed:
            return

    async def __aenter__(self) -> FakeFeed:
        last: Exception | None = None
        for port in THROWAWAY_PORTS:
            self.port = port
            try:
                self._server = await serve(
                    self._handler, "127.0.0.1", port, process_request=self._process_request
                )
                return self
            except OSError as exc:
                last = exc
        raise RuntimeError("no throwaway port free") from last

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 3)


def dist_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 111.2 * math.cos(math.radians(a[0])))
