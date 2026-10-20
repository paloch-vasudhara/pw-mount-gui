"""Shared helpers: synthetic PWI4 /status text and a mutable fake PWI4 transport."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway import settings as settings_mod

BASE = {
    "pwi4.version": "4.1.0",
    "response.timestamp_utc": "2026-10-20T04:00:00.000Z",
    "site.latitude_degs": "14.3479",
    "site.longitude_degs": "100.5656",
    "site.height_meters": "12",
    "site.lmst_hours": "5.5",
    "mount.is_connected": "true",
    "mount.julian_date": "2461333.5",
    "mount.ra_apparent_hours": "3.25",
    "mount.dec_apparent_degs": "10.0",
    "mount.ra_j2000_hours": "3.2",
    "mount.dec_j2000_degs": "9.9",
    "mount.target_ra_apparent_hours": "0",
    "mount.target_dec_apparent_degs": "0",
    "mount.azimuth_degs": "120.0",
    "mount.altitude_degs": "45.0",
    "mount.is_slewing": "false",
    "mount.is_tracking": "false",
    "mount.distance_to_sun_degs": "90",
    "mount.axis0_wrap_range_min_degs": "-180",
    "mount.axis0.is_enabled": "true",
    "mount.axis0.position_degs": "120.0",
    "mount.axis0.rms_error_arcsec": "1.0",
    "mount.axis0.dist_to_target_arcsec": "0",
    "mount.axis0.min_mech_position_degs": "-300",
    "mount.axis0.max_mech_position_degs": "300",
    "mount.axis0.max_velocity_degs_per_sec": "20",
    "mount.axis0.acceleration_degs_per_sec_sqr": "20",
    "mount.axis1.is_enabled": "true",
    "mount.axis1.position_degs": "45.0",
    "mount.axis1.rms_error_arcsec": "1.0",
    "mount.axis1.dist_to_target_arcsec": "0",
    "mount.axis1.min_mech_position_degs": "-40",
    "mount.axis1.max_mech_position_degs": "91",
    "mount.axis1.max_velocity_degs_per_sec": "20",
    "mount.axis1.acceleration_degs_per_sec_sqr": "20",
}


def status_text(over: dict[str, object] | None = None) -> str:
    """Build /status text, e.g. status_text({"mount.is_slewing": True})."""
    d = dict(BASE)
    for key, v in (over or {}).items():
        if key not in d:
            raise KeyError(key)
        d[key] = str(v).lower() if isinstance(v, bool) else str(v)
    return "\n".join(f"{k}={v}" for k, v in d.items()) + "\n"


class FakePwi4:
    """httpx.MockTransport backend. Records every request path (read-only proof)."""

    def __init__(self) -> None:
        self.text = status_text()
        self.down = False
        self.delay = 0.0  # seconds /status hangs before answering
        self.connect_timeout_after: float | None = None  # raise ConnectTimeout after N s
        self.paths: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.method + " " + request.url.path)
        if self.connect_timeout_after is not None:
            await asyncio.sleep(self.connect_timeout_after)
            raise httpx.ConnectTimeout("timed out")
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.down:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text=self.text)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Tests never touch the real %LOCALAPPDATA% settings file or the Credential Manager."""
    monkeypatch.setenv("PW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings_mod, "default_secret_store", settings_mod.MemorySecretStore)
