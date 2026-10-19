"""Shared helpers for the PWI4 simulation-mode harness."""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "pwi4"))

from pwi4_client import PWI4  # noqa: E402  (client lives outside the package; do not modify it)

SITE_LAT = 14.3479444
SITE_LON = 100.5655556
SITE_ELEV_M = 12.0

OFFSET_AXES = ("axis0", "axis1", "ra", "dec", "path", "transverse")


def wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def sep_deg(az1: float, alt1: float, az2: float, alt2: float) -> float:
    """Small-angle separation in degrees (az difference scaled by cos(alt))."""
    d_az = wrap180(az1 - az2) * math.cos(math.radians((alt1 + alt2) / 2))
    return math.hypot(d_az, alt1 - alt2)


def parse_utc(text: str | None) -> datetime | None:
    """Parse PWI4 '2026-10-19 09:45:26.170767' timestamps (UTC). None if invalid/zero."""
    if not text:
        return None
    try:
        dt = datetime.strptime(text.strip()[:26], "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            dt = datetime.strptime(text.strip()[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.year < 2000:
        return None
    return dt.replace(tzinfo=UTC)


def snap(st: Any, t: float) -> dict[str, Any]:
    """Compact sample of the fields we care about."""
    m = st.mount
    a0, a1 = m.axis0, m.axis1
    off = m.offsets
    return {
        "t": t,
        "resp_utc": st.response.timestamp_utc,
        "mount_utc": m.timestamp_utc,
        "az": m.azimuth_degs,
        "alt": m.altitude_degs,
        "slewing": m.is_slewing,
        "tracking": m.is_tracking,
        "ra_j2000": m.ra_j2000_hours,
        "dec_j2000": m.dec_j2000_degs,
        "p0": a0.position_degs,
        "p1": a1.position_degs,
        "mv0": a0.measured_velocity_degs_per_sec,  # always 0 in the simulator
        "mv1": a1.measured_velocity_degs_per_sec,
        "v0": 0.0,  # derived from position differences by Ctx._derive()
        "v1": 0.0,
        "d0": a0.dist_to_target_arcsec,
        "d1": a1.dist_to_target_arcsec,
        "off0_total": off.axis0_arcsec.total if off else None,
        "off0_rate": off.axis0_arcsec.rate if off else None,
        "off_ra_total": off.ra_arcsec.total if off else None,
    }


def stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    v = sorted(values)
    p95 = v[min(len(v) - 1, math.ceil(0.95 * len(v)) - 1)]
    return {
        "n": len(v),
        "mean": sum(v) / len(v),
        "p95": p95,
        "max": v[-1],
        "min": v[0],
    }


@dataclass
class Ctx:
    """Shared run context: client + live bookkeeping."""

    pwi: PWI4
    notes: list[str] = field(default_factory=list)
    connected: bool = False
    _last: dict[str, Any] | None = None

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    # --- status helpers -------------------------------------------------
    def status(self):
        return self.pwi.status()

    def _derive(self, s: dict[str, Any], tabs: float) -> dict[str, Any]:
        """Fill s['v0'/'v1'] (deg/s) from successive axis positions (sim reports 0 velocity)."""
        s["tabs"] = tabs
        prev = self._last
        if prev is not None and 0 < tabs - prev["tabs"] < 3.0:
            dt = tabs - prev["tabs"]
            s["v0"] = (s["p0"] - prev["p0"]) / dt
            s["v1"] = (s["p1"] - prev["p1"]) / dt
        self._last = s
        return s

    def sample(self) -> dict[str, Any]:
        t = time.perf_counter()
        st = self.pwi.status()
        return self._derive(snap(st, t), t)

    def poll(
        self,
        duration: float,
        *,
        interval: float = 0.1,
        until: Callable[[dict[str, Any]], bool] | None = None,
        t0: float | None = None,
    ) -> list[dict[str, Any]]:
        """Poll /status every `interval` s for up to `duration`; stop early if until(sample).

        Sample 't' is seconds since t0 (default: now), taken just before the request.
        """
        base = time.perf_counter() if t0 is None else t0
        end = time.perf_counter() + duration
        out: list[dict[str, Any]] = []
        nxt = time.perf_counter()
        while True:
            now = time.perf_counter()
            if now > end:
                break
            if now < nxt:
                time.sleep(nxt - now)
            req = time.perf_counter()
            st = self.pwi.status()
            s = self._derive(snap(st, req - base), req)
            s["lat_ms"] = (time.perf_counter() - req) * 1000
            out.append(s)
            if until and until(s):
                break
            nxt += interval
            if nxt < time.perf_counter():
                nxt = time.perf_counter()
        return out

    # --- connection -----------------------------------------------------
    def ensure_ready(self) -> None:
        st = self.pwi.status()
        if not st.mount.is_connected:
            self.pwi.mount_connect()
            self.connected = True
        st = self.pwi.status()
        for i, ax in enumerate(st.mount.axis):
            if not ax.is_enabled:
                self.pwi.mount_enable(i)

    def safe_state(self) -> None:
        """Best-effort: stop, tracking off, clear offsets. Never raises."""
        for fn in (self.pwi.mount_stop, self.pwi.mount_tracking_off):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.note(f"safe_state {fn.__name__} failed: {exc}")
        try:
            self.pwi.mount_offset(**{f"{a}_reset": 0 for a in OFFSET_AXES})
        except Exception as exc:  # noqa: BLE001
            self.note(f"safe_state offset reset failed: {exc}")
        try:
            self.pwi.mount_stop()
        except Exception:  # noqa: BLE001
            pass

    def goto_altaz_wait(self, alt: float, az: float, timeout: float = 60.0) -> dict[str, Any]:
        """Goto alt/az and wait until it has stopped slewing and axes are at rest."""
        t0 = time.perf_counter()
        self.pwi.mount_goto_alt_az(alt, az)
        samples = self.poll(timeout, t0=t0, until=lambda s: _settled(s) and s["t"] > 0.5)
        return {"samples": samples, "last": samples[-1] if samples else None}


def _settled(s: dict[str, Any], vtol: float = 0.02) -> bool:
    return not s["slewing"] and abs(s["v0"] or 0) < vtol and abs(s["v1"] or 0) < vtol


def settled(s: dict[str, Any], vtol: float = 0.02) -> bool:
    return _settled(s, vtol)


def utc_now() -> datetime:
    return datetime.now(UTC)
