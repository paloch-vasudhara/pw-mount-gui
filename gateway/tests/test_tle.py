"""T4.1a: the TLE client (httpx.MockTransport; never the real analysis service)."""

from __future__ import annotations

import httpx
import pytest

from gateway.config import Settings
from gateway.tle import (
    R_TLE_MISSING,
    R_TLE_UNAVAILABLE,
    TLE_MISSING_TTL_S,
    TLE_TTL_S,
    TleError,
    TleSource,
)

L1 = "1 25544U 98067A   26301.50000000  .00000000  00000-0  00000-0 0  9997"
L2 = "2 25544  51.6400 120.0000 0006000  80.0000  40.0000 15.50000000100001"
ROW = {
    "norad": 25544,
    "name": "ISS (ZARYA)",
    "tle_epoch": "2026-10-28T12:00:00",
    "tle_age_days": 1.5,
    "tle_line1": L1,
    "tle_line2": L2,
}


class Backend:
    """A scriptable analysis service + a controllable clock."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.mode = "ok"  # ok | 404 | 500 | timeout | refused | bad | notjson
        self.t = 1000.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.mode == "refused":
            raise httpx.ConnectError("refused", request=request)
        if self.mode == "404":
            return httpx.Response(404, json={"detail": "not found or decayed"})
        if self.mode == "500":
            return httpx.Response(503, text="db down")
        if self.mode == "notjson":
            return httpx.Response(200, text="<html>")
        if self.mode == "bad":
            return httpx.Response(200, json={"norad": 1, "name": "x"})  # no lines
        return httpx.Response(200, json=ROW)

    def source(self) -> TleSource:
        return TleSource(
            "http://analysis.invalid:1/",
            httpx.MockTransport(self.handler),
            clock=lambda: self.t,
        )


def test_fetch_parses_the_row_and_requests_the_norad_path():
    b = Backend()
    d = b.source().get(25544)
    assert (d.line1, d.line2, d.name, d.epoch) == (L1, L2, "ISS (ZARYA)", "2026-10-28T12:00:00")
    assert d.age_days == 1.5
    assert b.calls == ["/api/sats/25544"]


def test_cache_hit_within_ten_minutes_then_refetch():
    b = Backend()
    src = b.source()
    src.get(25544)
    b.t += TLE_TTL_S - 1
    src.get(25544)
    assert len(b.calls) == 1
    b.t += 2  # past the 10 min
    src.get(25544)
    assert len(b.calls) == 2


def test_cache_is_per_norad():
    b = Backend()
    src = b.source()
    src.get(1)
    src.get(2)
    src.get(1)
    assert b.calls == ["/api/sats/1", "/api/sats/2"]


def test_age_grows_while_cached():
    b = Backend()
    src = b.source()
    d = src.get(25544)
    assert d.age_at(b.t) == 1.5
    assert d.age_at(b.t + 86400) == pytest.approx(2.5)


def test_404_is_cached_30_s_with_the_not_found_reason():
    b = Backend()
    b.mode = "404"
    src = b.source()
    with pytest.raises(TleError) as e:
        src.get(7)
    assert (e.value.reason, e.value.code) == (R_TLE_MISSING, "tle_missing")
    assert R_TLE_MISSING == "Satellite not found or decayed"
    b.t += TLE_MISSING_TTL_S - 1
    with pytest.raises(TleError) as e:
        src.get(7)
    assert e.value.code == "tle_missing"
    assert len(b.calls) == 1  # served from the negative cache
    b.t += 2  # expired: asks again, and now it exists
    b.mode = "ok"
    assert src.get(7).line1 == L1
    assert len(b.calls) == 2


@pytest.mark.parametrize("mode", ["timeout", "refused", "500", "notjson", "bad"])
def test_failures_are_unavailable_and_never_cached(mode):
    b = Backend()
    b.mode = mode
    src = b.source()
    for n in (1, 2):
        with pytest.raises(TleError) as e:
            src.get(25544)
        assert (e.value.reason, e.value.code) == (R_TLE_UNAVAILABLE, "tle_unavailable")
        assert len(b.calls) == n  # asked again every time
    b.mode = "ok"  # recovers at once
    assert src.get(25544).line1 == L1


def test_a_failure_does_not_evict_a_good_entry_for_another_object():
    b = Backend()
    src = b.source()
    src.get(1)
    b.mode = "timeout"
    with pytest.raises(TleError):
        src.get(2)
    assert src.get(1).line1 == L1  # cached, no call
    assert len(b.calls) == 2


def test_client_timeout_is_two_seconds():
    assert TleSource("http://x")._client.timeout.read == 2.0
    TleSource("http://x").close()


def test_analysis_url_setting(monkeypatch):
    assert Settings().analysis_url == "http://127.0.0.1:8410"
    monkeypatch.delenv("PW_ANALYSIS_URL", raising=False)
    assert Settings.from_env().analysis_url == "http://127.0.0.1:8410"
    monkeypatch.setenv("PW_ANALYSIS_URL", "http://127.0.0.1:18499/")
    assert Settings.from_env().analysis_url == "http://127.0.0.1:18499"
