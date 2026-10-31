"""T4.1a: POST /api/mount/check for `track_object` (FakePwi4 + a MockTransport analysis service)
and the real WS command's refusal. Times are fixed (the TLE epoch), never the wall clock.
"""

from __future__ import annotations

import time

import httpx
import pytest

from gateway.control import R_NO_MOUNT, R_NOT_HOMED, R_SLEWING
from gateway.passes import compute_pass
from gateway.safety import R_SITE

from .conftest import FakePwi4
from .test_check import H, check
from .test_control import Win, audit_lines, running, wait_for
from .test_passes import GEO, LEO, LIM, SITE, T0, TS, info_at, sun_down
from .test_tle import L1, L2, Backend

NORAD = 25544
ARGS = {"norad": NORAD}
LEO_NOW = T0 + 78500  # mid-pass of the 62 deg LEO pass (see test_passes)
PASS_KEYS = {
    "alt", "az", "rising", "sun_sep", "t_rise", "t_in", "t_peak", "peak_alt", "t_out", "t_set",
    "phase", "t_r", "sun_sep_in",
}  # fmt: skip


def attach(app, backend: Backend, now: float):
    """Point the app's TLE client at the mock analysis service and pin the clock."""
    app.state.track.tle._client = httpx.Client(transport=httpx.MockTransport(backend.handler))
    app.state.track.tle.clock = lambda: backend.t
    app.state.track.clock = lambda: now


def at(app, now: float) -> None:
    app.state.track.clock = lambda: now


@pytest.fixture
def ref():
    return info_at(LEO, LEO_NOW)


def test_ok_verdict_fields_and_shape(ref):
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        d = check(tc, "track_object", ARGS)
        assert set(d) == {
            "ok", "reason", "code", "target", "detour", "pass", "tle_age_days", "notes",
        }  # fmt: skip
        assert d["ok"] is True and d["reason"] is None and d["code"] is None
        assert d["detour"] is None and d["notes"] == []
        assert d["pass"]["t_r"] >= LEO_NOW + 3  # row 7: the rendezvous of the track plan
        assert set(d["pass"]) == PASS_KEYS
        assert d["pass"]["phase"] == "track"
        assert d["target"] == {"alt": d["pass"]["alt"], "az": d["pass"]["az"]}
        assert d["target"]["alt"] == pytest.approx(ref.alt, abs=1e-6)
        assert d["pass"]["t_in"] == pytest.approx(ref.t_in, abs=1e-6)
        assert d["pass"]["peak_alt"] == pytest.approx(ref.peak_alt, abs=1e-6)
        assert d["pass"]["sun_sep"] > 15
        assert d["tle_age_days"] == pytest.approx(1.5, abs=0.01)
        assert b.calls == [f"/api/sats/{NORAD}"]
        assert fake.writes == []  # a check never writes to PWI4


def test_pre_position_then_each_rule_row(ref):
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)

        def verdict(now):
            at(app, now)
            return check(tc, "track_object", ARGS)

        d = verdict(ref.t_in - 40)  # rising toward Lim EL
        assert d["ok"] and d["pass"]["phase"] == "pre_position"
        assert d["target"]["alt"] < LIM

        d = verdict(ref.t_out - 10)  # 10 s left above Lim EL
        assert (d["ok"], d["code"], d["reason"]) == (
            False,
            "pass_too_short",
            "Pass ends in under 30 s",
        )
        assert d["pass"]["phase"] is None and d["pass"]["t_out"] == pytest.approx(
            ref.t_out, abs=0.5
        )

        d = verdict(ref.t_out - 31)  # row 6 passes, but the slew to the rendezvous eats the rest
        assert (d["ok"], d["code"]) == (False, "pass_too_short")  # row 7
        assert d["pass"]["t_r"] is None

        d = verdict(ref.t_out - 70)
        assert d["ok"] and d["pass"]["phase"] == "track"
        assert d["pass"]["t_r"] + 30 <= d["pass"]["t_out"]

        d = verdict(ref.t_out + 20)  # past the peak and below Lim EL
        assert (d["code"], d["ok"]) == ("setting", False)

        d = verdict(T0)  # LEO is -28 deg
        assert d["code"] == "below_horizon" and d["reason"] == "Below the horizon"
        assert d["pass"]["peak_alt"] is None and d["pass"]["t_rise"] is None
        assert d["target"]["alt"] < 0

        d = verdict(T0 + 37100)  # the 10 deg pass
        assert d["code"] == "peak_below_limit"
        assert d["reason"] == "Pass peaks below the station limit elevation"
        assert 0 < d["pass"]["peak_alt"] < LIM


def test_lead_window_through_the_check_endpoint(ref):
    """Q27b: below 0 deg, the Lim EL crossing 4 min ahead is allowed (pre_position, the meeting
    point planned with a wait); 6 min ahead is "Below the horizon"."""
    with running() as (tc, fake, app):
        attach(app, Backend(), LEO_NOW)
        at(app, ref.t_in - 240)
        d = check(tc, "track_object", ARGS)
        assert d["target"]["alt"] < 0
        assert d["ok"] and d["pass"]["phase"] == "pre_position"
        assert ref.t_in - 1 <= d["pass"]["t_r"] <= ref.t_in + 5  # the meeting point = the crossing
        at(app, ref.t_in - 360)
        d = check(tc, "track_object", ARGS)
        assert (d["ok"], d["code"]) == (False, "below_horizon")


def test_sun_row(ref, monkeypatch):
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", f"{ref.alt + 5:.3f},{ref.az:.3f}")
    with running() as (tc, fake, app):
        attach(app, Backend(), LEO_NOW)
        d = check(tc, "track_object", ARGS)
        assert (d["ok"], d["code"], d["reason"]) == (False, "sun", "Within 15° of the Sun")
        assert d["pass"]["sun_sep"] == pytest.approx(5.0, abs=0.1)
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")


def test_tle_missing_and_unavailable_and_recovery():
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        b.mode = "404"
        d = check(tc, "track_object", ARGS)
        assert (d["ok"], d["code"], d["reason"]) == (
            False,
            "tle_missing",
            "Satellite not found or decayed",
        )
        assert d["pass"] is None and d["tle_age_days"] is None and d["target"] is None
        b.mode = "timeout"
        b.t += 31  # the 404 entry expired
        d = check(tc, "track_object", ARGS)
        assert (d["code"], d["reason"]) == ("tle_unavailable", "Satellite data unavailable")
        b.mode = "ok"  # failures were not cached
        assert check(tc, "track_object", ARGS)["ok"] is True


def test_decayed_elements_are_unavailable():
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        app.state.track.tle._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={"tle_line1": L1, "tle_line2": L2.replace("0006000", "9900000")},
                )
            )
        )
        d = check(tc, "track_object", ARGS)
        assert (d["code"], d["reason"]) == ("tle_unavailable", "Satellite data unavailable")


def test_geo_continuous_object_is_ok():
    b = Backend()
    row = {"tle_line1": GEO[0], "tle_line2": GEO[1], "name": "GEO", "tle_age_days": 3.0}
    with running() as (tc, fake, app):
        attach(app, b, T0)
        app.state.track.tle._client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=row))
        )
        d = check(tc, "track_object", {"norad": 41838})
        assert d["ok"] and d["pass"]["phase"] == "track"
        assert d["pass"]["t_set"] is None and d["pass"]["t_out"] is None
        assert d["tle_age_days"] == pytest.approx(3.0, abs=0.01)


# ------------------------------------------------------------------ row 7: the track plan


def serve(app, tle, name="SYN", age=1.0):
    row = {"tle_line1": tle[0], "tle_line2": tle[1], "name": name, "tle_age_days": age}
    app.state.track.tle._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=row))
    )


def track_sun(offset: float, frac: float):
    """A Sun (alt, az) `offset` deg beside the LEO track, as the planner sees it from the fake
    mount's pose (the plan itself does not depend on the pose beyond the rendezvous time)."""
    from gateway.safety import AxisParams
    from gateway.trackplan import TrackMount

    from .test_trackplan import ok, sun_beside

    m = TrackMount(120.0, 45.0, AxisParams(20.0, 20.0), AxisParams(20.0, 20.0), (-300.0, 300.0))
    ref = ok(LEO, LEO_NOW, mount=m)
    return sun_beside(ref, frac, offset)


def test_row7_detour_and_rendezvous_fields(monkeypatch):
    alt, az = track_sun(3.0, 0.5)
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", f"{alt:.4f},{az:.4f}")
    with running() as (tc, fake, app):
        attach(app, Backend(), LEO_NOW)
        d = check(tc, "track_object", ARGS)
        assert d["ok"] is True, d
        assert d["detour"]["count"] == 1 and 5.0 < d["detour"]["seconds"] < 120.0
        assert set(d["detour"]) == {"seconds", "count"}
        assert d["notes"] == []
        assert LEO_NOW + 3 <= d["pass"]["t_r"] < d["pass"]["t_out"] - 30
        assert fake.writes == []
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")


def test_row7_no_detour_refuses_with_the_pass_wording(monkeypatch):
    ref = info_at(LEO, LEO_NOW)
    alt, az = track_sun(2.0, 1.0 - 1e-9)  # the pass ends inside the disc
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", f"{alt:.4f},{az:.4f}")
    with running() as (tc, fake, app):
        attach(app, Backend(), LEO_NOW)
        d = check(tc, "track_object", ARGS)
        assert (d["ok"], d["code"]) == (False, "sun_no_detour")
        assert d["reason"] == "Pass passes within 15° of the Sun; no safe detour"
        assert d["detour"] is None and d["notes"] == [] and d["pass"]["t_r"] is None
        assert d["pass"]["phase"] is None
        assert d["pass"]["t_out"] == pytest.approx(ref.t_out, abs=1e-6)
    monkeypatch.setenv("PW_TEST_SUN_ALTAZ", "-60,0")


def test_row7_near_zenith_pass_carries_the_note():
    from .test_passes import T0
    from .tlegen import ZENITH_89

    with running() as (tc, fake, app):
        attach(app, Backend(), T0 + 70)
        serve(app, ZENITH_89)
        d = check(tc, "track_object", ARGS)
        assert d["ok"] is True, d
        assert d["notes"] == ["Near-zenith pass: tracking lags near the peak"]
        assert d["pass"]["peak_alt"] > 87.0 and d["detour"] is None


def test_row7_is_cached_with_the_pass_and_replanned_when_stale():
    import gateway.trackcheck as tcheck

    calls = []
    real = tcheck.plan_track
    tcheck.plan_track = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        with running() as (tc, fake, app):
            attach(app, Backend(), LEO_NOW)
            first = check(tc, "track_object", ARGS)
            assert first["ok"] and len(calls) == 1
            at(app, LEO_NOW + 1)
            second = check(tc, "track_object", ARGS)
            assert len(calls) == 1 and second["pass"]["t_r"] == first["pass"]["t_r"]
            at(app, LEO_NOW + 11)  # PLAN_TTL_S
            third = check(tc, "track_object", ARGS)
            assert len(calls) == 2
            at(app, third["pass"]["t_r"] + 1)  # the rendezvous has gone by: replanned
            check(tc, "track_object", ARGS)
            assert len(calls) == 3
            # a refusal of rows 1-6 never runs the planner
            at(app, LEO_NOW + 78805 - 78500 + 400)
            assert check(tc, "track_object", ARGS)["ok"] is False
            assert len(calls) == 3
    finally:
        tcheck.plan_track = real


def test_row7_runs_only_when_the_mount_status_is_known():
    # without a mount (the evaluate() call of T4.1) the verdict is rows 1-6 only
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        site = app.state.poller.pwi4_site()
        r = app.state.track.evaluate(NORAD, site, 15.0, lambda: (-60.0, 0.0))
        assert r.reason is None and r.detour is None and r.notes == []
        assert r.pass_info["t_r"] is None


# ------------------------------------------------------------------ row 0: the mount guards win


def assert_guard_wins(tc, backend, code, reason):
    d = check(tc, "track_object", ARGS)
    assert (d["ok"], d["code"], d["reason"]) == (False, code, reason), d
    assert d["pass"] is None and d["tle_age_days"] is None and d["target"] is None
    assert backend.calls == []  # not even the TLE is fetched


def test_guard_mount_not_connected():
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        fake.set(mount__is_connected=False)
        wait_for(lambda: not app.state.control.fresh_status().mount.is_connected)
        assert_guard_wins(tc, b, "no_mount", R_NO_MOUNT)


def test_guard_not_homed():
    b = Backend()
    with running(homed=False) as (tc, fake, app):
        attach(app, b, LEO_NOW)
        wait_for(lambda: app.state.control.homed.blocking)
        assert_guard_wins(tc, b, "not_homed", R_NOT_HOMED)
        assert fake.writes == []


def test_guard_pwi4_unreachable():
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        fake.down = True
        wait_for(lambda: app.state.control.fresh_status() is None)
        d = check(tc, "track_object", ARGS)
        assert d["code"] == "no_pwi4" and d["ok"] is False and d["pass"] is None
        assert b.calls == []


def test_guard_site_mismatch():
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        fake.set(site__latitude_degs=20.0)
        wait_for(lambda: (app.state.poller.current_site_check() or object()).ok is False)
        assert_guard_wins(tc, b, "site_mismatch", R_SITE)


def test_guard_slewing():
    class Moving(FakePwi4):  # SLEWING needs the axis to actually move (P2)
        moving = False
        n = 0

        async def handler(self, request):
            if self.moving and request.url.path == "/status":
                self.n += 1
                p = 120.0 + 0.5 * self.n
                self.set(
                    mount__is_slewing=True,
                    mount__axis0__position_degs=p,
                    mount__azimuth_degs=p,
                )
            return await super().handler(request)

    b = Backend()
    fake = Moving()
    with running(fake) as (tc, _, app):
        attach(app, b, LEO_NOW)
        fake.moving = True
        wait_for(lambda: app.state.poller.latest.state == "SLEWING")
        assert_guard_wins(tc, b, "slewing", R_SLEWING)


def test_guard_axis_disabled():
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        fake.set(mount__axis1__is_enabled=False)
        wait_for(lambda: not app.state.control.fresh_status().mount.axis1.is_enabled)
        assert_guard_wins(tc, b, "axis_disabled", "Axis 1 disabled")


def test_guard_sequence_busy_and_starting():
    import types

    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        app.state.control.seq._task = types.SimpleNamespace(done=lambda: False)
        try:
            assert_guard_wins(tc, b, "sequence_busy", "Busy: homing")
        finally:
            app.state.control.seq._task = None
        app.state.control.ready = False
        assert_guard_wins(tc, b, "starting", "Gateway starting up (checking mount state)")
        app.state.control.ready = True


# ------------------------------------------------------------------ schema / auth / WS


@pytest.mark.parametrize(
    "args",
    [{}, {"norad": 0}, {"norad": -5}, {"norad": "25544"}, {"norad": 25544.5}, {"norad": True},
     {"norad": 25544, "x": 1}],
)  # fmt: skip
def test_check_bad_track_args_are_422(args):
    with running() as (tc, fake, app):
        r = tc.post("/api/mount/check", json={"cmd": "track_object", "args": args}, headers=H)
        assert r.status_code == 422, r.text
        assert fake.writes == []


def test_other_commands_keep_their_exact_body():
    with running() as (tc, fake, _):
        d = check(tc, "goto_altaz", {"alt": 45.0, "az": 125.0})
        assert set(d) == {"ok", "reason", "code", "target", "detour"}  # no pass fields


def test_real_ws_track_object_refusal_equals_the_check(tmp_path):
    """T4.2c: the real command runs the same verdict as the check; a refusal writes nothing."""
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, T0)  # LEO is below the horizon
        d = check(tc, "track_object", ARGS)
        w = Win(tc)
        ack = w.send("track_object", ARGS, "tr1")
        assert ack["ok"] is False and ack["reason"] == d["reason"] == "Below the horizon"
        assert ack["code"] == d["code"] == "below_horizon" and ack["cmd"] == "track_object"
        assert fake.writes == []  # nothing went to PWI4
        assert not app.state.control.motion_commanded and not app.state.control.trackx.active
        ev = audit_lines(tmp_path)
        rows = [e for e in ev if e.get("cmd") == "track_object"]
        assert len(rows) == 1 and rows[0]["result"] == "refused"
        assert not any(e["event"] in ("safety_refusal", "goto_start") for e in ev)
        # a bad argument is still a validation refusal
        bad = w.send("track_object", {"norad": 0}, "tr2")
        assert bad["ok"] is False and bad["reason"].startswith("Invalid arguments")
        w.close()


def test_check_requires_auth():
    with running() as (tc, fake, _):
        body = {"cmd": "track_object", "args": ARGS}
        assert tc.post("/api/mount/check", json=body).status_code == 401


# ------------------------------------------------------------------ cache + latency


def test_cached_pass_is_reused_and_cheap(capsys):
    b = Backend()
    calls = []
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        import gateway.trackcheck as tcheck

        real = tcheck.compute_pass
        tcheck.compute_pass = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        try:
            check(tc, "track_object", ARGS)  # warm: computes
            assert len(calls) == 1
            t0 = time.perf_counter()
            for k in range(1, 21):
                at(app, LEO_NOW + k)  # 1 Hz checks
                check(tc, "track_object", ARGS)
            ms = (time.perf_counter() - t0) * 1000 / 20
            assert len(calls) == 1 and b.calls == [f"/api/sats/{NORAD}"]
            with capsys.disabled():
                print(f"\n  cached check round trip: {ms:.1f} ms avg (TestClient)")
            assert ms < 50.0
            at(app, LEO_NOW + 61)  # past the 60 s: recomputed
            check(tc, "track_object", ARGS)
            assert len(calls) == 2
            at(app, T0 + 100)  # below the horizon, then it rises: the pass-less entry is replaced
            check(tc, "track_object", ARGS)
            n = len(calls)
            at(app, LEO_NOW)
            assert check(tc, "track_object", ARGS)["pass"]["peak_alt"] is not None
            assert len(calls) > n
        finally:
            tcheck.compute_pass = real


def test_live_fields_move_between_cached_checks():
    b = Backend()
    with running() as (tc, fake, app):
        attach(app, b, LEO_NOW)
        a = check(tc, "track_object", ARGS)["pass"]
        at(app, LEO_NOW + 10)
        c = check(tc, "track_object", ARGS)["pass"]
        assert c["alt"] != a["alt"] and c["az"] != a["az"]
        assert c["t_in"] == a["t_in"] and c["t_set"] == a["t_set"]
        ref = compute_pass(L1, L2, SITE, LIM, LEO_NOW + 10, sun_down, TS)
        assert c["alt"] == pytest.approx(ref.alt, abs=1e-6)
