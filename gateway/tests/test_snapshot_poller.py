import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gateway.config import Settings
from gateway.ephem import Ephem, angular_sep_deg
from gateway.hub import Hub
from gateway.poller import Poller
from gateway.pwi4_io import PollResult, parse
from gateway.snapshot import build_snapshot, jd_from_utc, lmst_hours_from

from .conftest import FakePwi4, status_text

EPH = Path(__file__).resolve().parents[2] / "data" / "ephemeris" / "de421.bsp"
HAVE_EPH = EPH.exists()


def make_poller(fake: FakePwi4) -> Poller:
    s = Settings(ephemeris=EPH)
    return Poller(s, Hub(), Ephem(s.ephemeris), fake.transport)


def run(coro):
    return asyncio.run(coro)


def test_snapshot_from_status_text():
    fake = FakePwi4()
    p = make_poller(fake)
    snap = run(p.poll_once(now_mono=0.0))
    assert snap.state == "STOPPED"
    assert snap.pwi4.reachable and snap.pwi4.version == "4.1.0"
    assert snap.site.lat == pytest.approx(14.3479) and snap.site.source == "pwi4"
    # fixture JD / LMST disagree with the real clock -> the gateway's own values are used
    assert snap.time.mjd == pytest.approx(jd_from_utc(datetime.now(UTC)) - 2400000.5, abs=1e-3)
    assert snap.mount.az == 120.0 and snap.mount.alt == 45.0
    assert snap.axes[0].position_deg == 120.0 and snap.axes[1].max_deg == 91.0
    # Hour angle is wrapped to [-12, 12) h, so compare against the wrapped LMST − RA
    # (an unwrapped comparison fails whenever LMST − RA > 12 h, i.e. depending on the clock).
    expected_ha = (snap.time.lmst_hours - 3.25 + 12.0) % 24.0 - 12.0
    assert snap.hour_angle_h == pytest.approx(expected_ha)
    assert fake.paths == ["GET /status"]  # read-only


def test_hour_angle_wraps():
    fake = FakePwi4()
    fake.text = status_text({"mount.ra_apparent_hours": 23.0})
    snap = run(make_poller(fake).poll_once(now_mono=0.0))
    d = snap.time.lmst_hours - 23.0
    assert -12 <= snap.hour_angle_h < 12
    assert (snap.hour_angle_h - d) % 24 == pytest.approx(0, abs=1e-6) or (
        (snap.hour_angle_h - d) % 24 == pytest.approx(24, abs=1e-6)
    )


def test_unreachable_then_recovery():
    fake = FakePwi4()
    p = make_poller(fake)

    async def go():
        a = await p.poll_once(now_mono=0.0)
        fake.down = True
        b = await p.poll_once(now_mono=0.1)
        fake.down = False
        c = await p.poll_once(now_mono=0.2)
        return a, b, c

    a, b, c = run(go())
    assert a.state == "STOPPED"
    assert b.state == "DISCONNECTED" and not b.pwi4.reachable
    assert b.state_reason == "PWI4 not running / not reachable"
    assert c.state == "STOPPED" and c.pwi4.reachable
    assert [x.seq for x in (a, b, c)] == [1, 2, 3]


def test_mount_disconnected_and_fault():
    fake = FakePwi4()
    p = make_poller(fake)
    fake.text = status_text({"mount.is_connected": False})
    assert run(p.poll_once(now_mono=0.0)).state_reason == "mount not connected"
    fake.text = status_text({"mount.axis1.is_enabled": False})
    snap = run(p.poll_once(now_mono=1.0))
    assert snap.state == "FAULT" and snap.state_reason == "axis 1 disabled"


def test_slew_then_p2_freeze_then_tracking():
    fake = FakePwi4()
    p = make_poller(fake)

    def t(az, slewing, tracking):
        fake.text = status_text(
            {
                "mount.axis0.position_degs": az,
                "mount.is_slewing": slewing,
                "mount.is_tracking": tracking,
            }
        )

    async def go():
        out = []
        t(0.0, False, False)
        out.append(await p.poll_once(now_mono=0.0))
        t(2.0, True, True)  # 20 deg/s, tracking flag also true (P3)
        out.append(await p.poll_once(now_mono=0.1))
        t(4.0, True, True)
        out.append(await p.poll_once(now_mono=0.2))
        for i in range(1, 6):  # frozen; is_slewing still true (P2)
            t(4.0, True, True)
            out.append(await p.poll_once(now_mono=0.2 + 0.1 * i))
        return out

    out = run(go())
    assert [o.state for o in out[:3]] == ["STOPPED", "SLEWING", "SLEWING"]
    assert out[1].axes[0].velocity_deg_s == pytest.approx(20.0)
    # frozen from 0.2; last moving sample at 0.2 -> still SLEWING at 0.3/0.4/0.5, off at 0.6
    assert [o.state for o in out[3:]] == ["SLEWING", "SLEWING", "SLEWING", "TRACKING", "TRACKING"]


def test_hub_receives_envelope():
    fake = FakePwi4()
    p = make_poller(fake)

    async def go():
        c = p.hub.add()
        await p.poll_once(now_mono=0.0)
        return json.loads(c.queue.get_nowait())

    msg = run(go())
    assert set(msg) == {"type", "seq", "ts", "data"}
    assert msg["type"] == "status" and msg["seq"] == 1 and msg["data"]["state"] == "STOPPED"


def test_angular_sep():
    assert angular_sep_deg(0, 0, 90, 0) == pytest.approx(90)
    assert angular_sep_deg(10, 20, 10, 20) == pytest.approx(0, abs=1e-6)
    assert angular_sep_deg(0, 90, 123, 0) == pytest.approx(90)


@pytest.mark.skipif(not HAVE_EPH, reason="run tools/fetch_ephemeris.py")
def test_sun_moon_in_snapshot():
    fake = FakePwi4()
    snap = run(make_poller(fake).poll_once(now_mono=0.0))
    assert -90 <= snap.sun.alt <= 90 and 0 <= snap.sun.az < 360
    assert 0 <= snap.moon.illumination <= 1
    assert 0 <= snap.sun_distance_deg <= 180


NOW = datetime(2026, 10, 20, 4, 0, 0, tzinfo=UTC)


def build(over, now=NOW, vel=(0.0, 0.0)):
    res = PollResult(True, status=parse(status_text(over)))
    sm = None
    return build_snapshot(seq=1, result=res, vel=vel, moving=False, sunmoon=sm, now_utc=now)


def test_lmst_matches_skyfield():
    from skyfield.api import load

    ts = load.timescale()
    for when, lon in ((NOW, 100.5656), (datetime(2027, 3, 1, 17, 30, tzinfo=UTC), -70.0)):
        expect = (ts.from_datetime(when).gmst + lon / 15.0) % 24.0
        got = lmst_hours_from(jd_from_utc(when), lon)
        d = (got - expect + 12) % 24 - 12
        assert abs(d) < 0.001, (got, expect)


def test_disconnected_mount_zeros_are_nulled_and_time_is_valid():
    snap = build(
        {
            "mount.is_connected": False,
            "mount.julian_date": 0,
            "site.lmst_hours": 0,
            "mount.azimuth_degs": 0,
            "mount.altitude_degs": 0,
            "mount.ra_apparent_hours": 0,
            "mount.distance_to_sun_degs": 0,
            "mount.axis0.position_degs": 0,
        }
    )
    assert snap.state == "DISCONNECTED" and snap.state_reason == "mount not connected"
    assert snap.time.jd == pytest.approx(jd_from_utc(NOW))
    assert snap.time.mjd == pytest.approx(jd_from_utc(NOW) - 2400000.5)
    assert snap.time.lmst_hours == pytest.approx(lmst_hours_from(jd_from_utc(NOW), 100.5656))
    m = snap.mount
    for f in (
        "az", "alt", "ra_j2000_h", "dec_j2000_deg", "ra_apparent_h", "dec_apparent_deg",
        "target_ra_apparent_h", "target_dec_apparent_deg", "distance_to_sun_deg_pwi4",
    ):  # fmt: skip
        assert getattr(m, f) is None, f
    assert snap.hour_angle_h is None and snap.sun_distance_deg is None
    assert all(a.position_deg is None and a.velocity_deg_s is None for a in snap.axes)
    assert snap.axes[0].max_deg == 300.0  # configuration still reported


def test_disconnected_mount_keeps_sun_moon():
    res = PollResult(True, status=parse(status_text({"mount.is_connected": False})))
    from gateway.ephem import SunMoon

    snap = build_snapshot(
        seq=1, result=res, vel=(0, 0), moving=False, now_utc=NOW,
        sunmoon=SunMoon(10.0, 20.0, 30.0, 40.0, 0.5),
    )  # fmt: skip
    assert snap.sun.alt == 10.0 and snap.moon.illumination == 0.5
    assert snap.sun_distance_deg is None


def test_connected_values_pass_through_when_they_agree_with_our_clock():
    jd = jd_from_utc(NOW)
    lmst = lmst_hours_from(jd, 100.5656)
    snap = build(
        {
            "mount.julian_date": jd + 0.5 / 86400,
            "site.lmst_hours": lmst + 0.005,
            "mount.ra_apparent_hours": 3.25,
        }
    )
    assert snap.time.jd == pytest.approx(jd + 0.5 / 86400)
    assert snap.time.lmst_hours == pytest.approx(lmst + 0.005)
    assert snap.mount.az == 120.0 and snap.mount.alt == 45.0
    assert snap.axes[0].position_deg == 120.0 and snap.axes[0].velocity_deg_s == 0.0
    assert snap.sun_distance_deg is None  # no sun supplied
    assert snap.hour_angle_h == pytest.approx(lmst + 0.005 - 3.25)


def test_connected_but_disagreeing_pwi4_time_uses_ours():
    jd = jd_from_utc(NOW)
    snap = build({"mount.julian_date": jd + 5 / 86400, "site.lmst_hours": 0.0})
    assert snap.time.jd == pytest.approx(jd)
    assert snap.time.lmst_hours == pytest.approx(lmst_hours_from(jd, 100.5656))


def test_unreachable_snapshot_has_valid_time_and_null_pointing():
    snap = build_snapshot(
        seq=1, result=PollResult(False, kind="unreachable"), vel=(0, 0), moving=False,
        sunmoon=None, now_utc=NOW,
    )  # fmt: skip
    assert snap.time.mjd == pytest.approx(jd_from_utc(NOW) - 2400000.5)
    assert snap.time.lmst_hours is None  # no site known yet
    assert snap.mount.az is None and snap.axes[0].position_deg is None


def test_stale_snapshot_keeps_site_and_lmst():
    res = PollResult(True, status=parse(status_text()))
    snap = build_snapshot(
        seq=1, result=res, vel=(0, 0), moving=False, sunmoon=None, now_utc=NOW,
        down_reason="PWI4 not responding",
    )  # fmt: skip
    assert snap.state == "DISCONNECTED" and snap.site.lon == pytest.approx(100.5656)
    assert snap.time.lmst_hours == pytest.approx(lmst_hours_from(jd_from_utc(NOW), 100.5656))
    assert snap.mount.az is None
