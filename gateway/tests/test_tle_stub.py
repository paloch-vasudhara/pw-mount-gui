"""T4.2c: the TLE stub (tests/tle_stub.py) speaks the analysis service's /api/sats/{norad} shape and
works with the gateway's own TleSource."""

from __future__ import annotations

import httpx

from gateway.tle import CODE_TLE_MISSING, TleError, TleSource

from .tle_stub import TleStub
from .tlegen import PEAK_70, ZENITH_89


def test_stub_serves_updates_and_404s():
    with TleStub() as stub:
        assert stub.url.startswith("http://127.0.0.1:") and stub.port > 0
        r = httpx.get(f"{stub.url}/api/sats/99001")
        assert r.status_code == 404 and "detail" in r.json()
        stub.set(99001, "SYN-A", *PEAK_70, epoch="2026-10-28T12:00:00", age_days=0.5)
        row = httpx.get(f"{stub.url}/api/sats/99001").json()
        assert row == {
            "norad": 99001, "name": "SYN-A", "tle_line1": PEAK_70[0], "tle_line2": PEAK_70[1],
            "tle_epoch": "2026-10-28T12:00:00", "tle_age_days": 0.5,
        }  # fmt: skip
        stub.set(99001, "SYN-B", *ZENITH_89)  # runtime update
        assert httpx.get(f"{stub.url}/api/sats/99001").json()["tle_line2"] == ZENITH_89[1]
        stub.remove(99001)
        assert httpx.get(f"{stub.url}/api/sats/99001").status_code == 404
        assert httpx.get(f"{stub.url}/other").status_code == 404
        assert httpx.get(f"{stub.url}/api/sats/abc").status_code == 404
        assert stub.calls[0] == "/api/sats/99001" and len(stub.calls) == 6


def test_gateway_tle_source_reads_the_stub():
    with TleStub() as stub:
        stub.set(99001, "SYN-A", *PEAK_70, epoch="2026-10-28T12:00:00", age_days=1.25)
        src = TleSource(stub.url)
        d = src.get(99001)
        assert (d.name, d.line1, d.line2, d.epoch, d.age_days) == (
            "SYN-A", PEAK_70[0], PEAK_70[1], "2026-10-28T12:00:00", 1.25,
        )  # fmt: skip
        try:
            src.get(5)
        except TleError as exc:
            assert exc.code == CODE_TLE_MISSING
        else:
            raise AssertionError("expected TleError")
        src.close()
