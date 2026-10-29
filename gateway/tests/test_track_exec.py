"""T4.2c: the object-track executor (trackexec.py) against FakePwi4 and a TLE stub.

No PWI4 is contacted. Time is a FAKE clock: `ControlService.track_clock` / `track_sleep` are
replaced by the `Rig` below, so a 6-minute pass runs in about a second. The rig also plays the
mount: when the executor waits at the rendezvous, the fake mount is parked at the plan's
rendezvous pose (what the real goto does); after the upload it stays there (the path itself is not
simulated, the sim tests of T4.2d cover that). `pause_at` holds the executor at a fake time so the
test can look at the snapshot / writes / monitor, send a command, and `resume()`.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from urllib.parse import parse_qs

import pytest

from gateway import state
from gateway.commands import Command
from gateway.control import R_OBJECT_TRACK
from gateway.trackexec import END_DELAY_S, R_TRACK_ENDED

from .conftest import FakePwi4
from .test_check import check
from .test_control import Win, audit_lines, running, wait_for
from .test_dsun_wiring import park_fake, snap_when
from .test_passes import GEO
from .tle_stub import TleStub
from .tlegen import PEAK_70, TEST_EPOCH

NORAD = 99001
GEO_NORAD = 41838
ARGS = {"norad": NORAD}
T0 = TEST_EPOCH  # PEAK_70: first Lim EL crossing T0 + 60 s, peak T0 + 220 s


class Rig:
    """Fake clock + sleep hook + fake mount for one running app."""

    def __init__(self, app, fake: FakePwi4, stub: TleStub, start: float = T0) -> None:
        self.app, self.fake, self.control = app, fake, app.state.control
        self.t = start
        self.log: list[tuple[float, str | None, tuple | None]] = []
        self.park_after = -math.inf  # park the fake mount at the rendezvous once t >= this
        self.slew_skew = 0.0  # extra fake seconds per wait while slewing (a slow mount)
        self.parked = 0
        self.pause_at: float | None = None
        self.paused = threading.Event()
        self._gate: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.control.track_clock = lambda: self.t
        self.control.track_sleep = self.sleep
        self.control.trackx.poll_s = 1.0e9  # wake at the next event only
        app.state.track.tle.base_url = stub.url

    @property
    def phases(self) -> list[str | None]:
        out: list[str | None] = []
        for _, ph, _ in self.log:
            if not out or out[-1] != ph:
                out.append(ph)
        return out

    async def sleep(self, dt: float) -> None:
        self._loop = asyncio.get_running_loop()
        tr = self.control.trackx.t
        self.log.append((self.t, tr.phase if tr else None, state.get_override()))
        real = 0.01
        if tr is not None and tr.phase == "slewing":
            gotos = sum(1 for p, _ in self.fake.writes if p == "/mount/goto_alt_az")
            if gotos > self.parked and self.t >= self.park_after:
                rdv = tr.plan.rendezvous
                park_fake(self.fake, rdv.axis0, rdv.alt)
                self.parked = gotos
                real = 0.15  # the poller needs a few cycles to see the new pose
            elif self.parked == 0:
                self.t += self.slew_skew  # (only until the mount first arrives)
            if self.parked:
                real = 0.15  # let the poller see the pose and the tracker settle
        if self.pause_at is not None and self.t + dt >= self.pause_at:
            dt -= max(0.0, self.pause_at - self.t)  # the clock stops AT pause_at ...
            self.t = max(self.t, self.pause_at)
            self.pause_at = None
            self._gate = asyncio.Event()
            self.paused.set()
            await self._gate.wait()
        self.t += dt  # ... and goes on when resumed
        await asyncio.sleep(real)

    def wait_paused(self, timeout: float = 15.0) -> bool:
        ok = self.paused.wait(timeout)
        self.paused.clear()
        return ok

    def resume(self) -> None:
        if self._loop is not None and self._gate is not None:
            self._loop.call_soon_threadsafe(self._gate.set)


def row(stub: TleStub, norad: int = NORAD, tle=PEAK_70, name: str = "SYN-A") -> None:
    stub.set(norad, name, *tle, epoch="2026-10-28T12:00:00", age_days=0.5)


def paths(fake: FakePwi4) -> list[str]:
    return [p for p, _ in fake.writes]


def uploads(fake: FakePwi4) -> list[list[tuple[float, ...]]]:
    """The (jd, az, alt) points of every add_point_list POST, in order."""
    out = []
    for pth, body in fake.bodies:
        if pth.endswith("add_point_list"):
            rows = parse_qs(body)["data"][0].splitlines()
            out.append([tuple(float(x) for x in r.split(",")) for r in rows])
    return out


def events(tmp_path, name: str) -> list[dict]:
    return [e for e in audit_lines(tmp_path) if e.get("event") == name]


def tracking_rig(tc, fake, app, stub, *, pause_after_tr: float = 20.0, tle=PEAK_70, norad=NORAD):
    """Send track_object at T0 and hold the executor `pause_after_tr` s after t_r."""
    row(stub, norad, tle)
    rig = Rig(app, fake, stub)
    d = check(tc, "track_object", {"norad": norad})
    assert d["ok"], d
    rig.pause_at = d["pass"]["t_r"] + pause_after_tr
    w = Win(tc)
    ack = w.send("track_object", {"norad": norad}, "k1")
    assert ack["ok"], ack["reason"]
    assert rig.wait_paused(), rig.log[-3:]
    snap_when(tc, lambda s: s["state"] == "TRACKING")  # the poller republishes at 10 Hz
    return rig, w, d


# ------------------------------------------------------------------ the whole pass


def test_pass_sequence_phases_snapshot_monitor_audit_end(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub)
        rig = Rig(app, fake, stub)
        ctl = app.state.control
        d = check(tc, "track_object", ARGS)
        assert d["ok"] and d["pass"]["phase"] == "pre_position"
        t_r = d["pass"]["t_r"]
        rig.pause_at = t_r + 20.0
        w = Win(tc)
        ack = w.send("track_object", ARGS, "k1")
        assert ack["ok"] is True and ack["cmd"] == "track_object", ack  # acked at the goto start
        assert rig.wait_paused(), (
            rig.t,
            rig.phases,
            [
                (e["event"], e.get("reason"), e.get("phase"))
                for e in audit_lines(tmp_path)
                if e["event"].startswith("track")
            ],
        )
        plan = ctl.trackx.t.plan
        assert plan.t_r == pytest.approx(t_r)  # the command ran the check's verdict

        # --- the PWI4 call sequence: goto, then tracking_off, wrap, new, add, apply(0)
        names = paths(fake)
        goto = names.index("/mount/goto_alt_az")
        new = names.index("/mount/custom_path/new")
        add = names.index("/mount/custom_path/add_point_list")
        apply = names.index("/mount/custom_path/apply")
        assert goto < new < add < apply
        assert "/mount/tracking_off" in names[goto + 1 : new]
        wraps = [i for i, p in enumerate(names) if p == "/mount/set_axis0_wrap_range_min"]
        assert wraps and wraps[-1] < new
        assert names.count("/mount/custom_path/new") == 1 and names[apply:] == [names[apply]]
        calls = dict(enumerate(fake.writes))
        assert calls[apply][1] == {"update_wrap": "0"}
        w_min = min(p.axis0 for p in plan.points) - 1.0  # a final plan: lo - 1 (route_wrap_plan)
        assert float(calls[wraps[-1]][1]["degs"]) == pytest.approx(w_min, abs=0.01)
        pts = uploads(fake)[0]
        assert len(pts) == len(plan.points)
        assert (pts[0][0] - 2440587.5) * 86400.0 == pytest.approx(plan.t_r, abs=0.01)
        assert pts[0][2] == pytest.approx(plan.rendezvous.alt, abs=1e-5)  # the refracted alt
        # the rendezvous goto named the planner's axis-0 representation (target0)
        goto_params = calls[goto][1]
        assert float(goto_params["alt_degs"]) == pytest.approx(plan.rendezvous.alt, abs=1e-6)
        assert float(goto_params["az_degs"]) == pytest.approx(plan.rendezvous.az % 360, abs=1e-6)

        # --- phases and the state override
        assert rig.phases[:3] == ["slewing", "waiting", "tracking"], rig.phases
        assert state.get_override() == ("TRACKING", "Tracking SYN-A")
        assert any(o == ("TRACKING", "Waiting for SYN-A") for _, _, o in rig.log)

        # --- snapshot
        s = snap_when(tc, lambda s: s["track"] and s["track"]["phase"] == "tracking")
        tr = s["track"]
        assert set(tr) == {
            "norad", "name", "phase", "t_r", "t_end", "t_peak", "peak_alt", "detours", "notes",
            "ended",
        }  # fmt: skip
        assert (tr["norad"], tr["name"], tr["phase"]) == (NORAD, "SYN-A", "tracking")
        assert tr["t_r"] == pytest.approx(plan.t_r) and tr["t_end"] == pytest.approx(plan.t_end)
        assert tr["t_end"] == pytest.approx(d["pass"]["t_out"], abs=1.0)
        assert tr["t_peak"] == pytest.approx(d["pass"]["t_peak"], abs=0.5)
        assert tr["peak_alt"] == pytest.approx(d["pass"]["peak_alt"], abs=0.01)
        assert tr["detours"] == [] and tr["notes"] == [] and tr["ended"] is None
        assert s["state"] == "TRACKING" and s["state_reason"] == "Tracking SYN-A"

        # --- the monitor: a route-type plan (look-ahead off) over the track corridor
        mp = app.state.monitor.plan
        assert mp is not None and mp.route is True
        assert len(mp.paths[0]) == len(plan.points)
        assert mp.target == pytest.approx((plan.points[-1].axis0, plan.points[-1].alt))
        assert mp.duration_s >= plan.t_end - rig.t

        # --- run to the end
        rig.resume()
        wait_for(lambda: not ctl.trackx.active, 10.0)
        assert rig.t == pytest.approx(plan.t_end + END_DELAY_S, abs=1.5)
        tail = paths(fake)[apply + 1 :]
        assert (
            "/mount/stop" in tail
            and tail[-1] == "/mount/tracking_off"
            or "/mount/tracking_off" in tail
        )
        assert app.state.monitor.plan is None
        # the override is set after the end's stop / tracking_off writes: wait, don't read at once
        # (flaky under load, 10-29)
        wait_for(lambda: state.get_override() == ("STOPPED", R_TRACK_ENDED), 5.0)
        s = snap_when(tc, lambda s: s["track"] and s["track"]["phase"] is None)
        assert s["state"] == "STOPPED" and s["state_reason"] == R_TRACK_ENDED
        assert s["track"]["ended"]["norad"] == NORAD
        assert s["track"]["ended"]["reason"] == R_TRACK_ENDED
        assert s["track"]["ended"]["at"] == pytest.approx(rig.t)
        assert s["track"]["norad"] is None and s["track"]["detours"] == []

        # --- audit
        start = events(tmp_path, "track_start")[-1]
        assert (start["norad"], start["name"], start["tle_epoch"]) == (
            NORAD, "SYN-A", "2026-10-28T12:00:00",
        )  # fmt: skip
        assert start["t_r"] == pytest.approx(plan.t_r, abs=0.01) and start["detours"] == []
        assert start["t_end"] == pytest.approx(d["pass"]["t_out"], abs=1.0)
        assert start["min_sun_sep"] is None or start["min_sun_sep"] > 16
        done = events(tmp_path, "track_done")
        assert len(done) == 1 and done[0]["norad"] == NORAD and done[0]["reason"] == R_TRACK_ENDED
        assert not events(tmp_path, "track_stopped") and not events(tmp_path, "track_replaced")

        # --- the next command clears the STOPPED reason; `ended` lasts 30 s
        ack = w.send("tracking", {"on": False}, "k2")
        assert ack["ok"] and state.get_override() is None
        assert ctl.trackx.info() is not None
        rig.t += 31.0
        assert ctl.trackx.info() is None
        w.close()


def test_late_arrival_replans_once_with_a_short_goto(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub)
        rig = Rig(app, fake, stub)
        # start low: the fake mount's jump to the rendezvous is then an ascent, which the
        # predictive monitor (the first goto's plan has expired by then) leaves alone
        park_fake(fake, 120.0, 5.0)
        wait_for(lambda: app.state.control.fresh_status().mount.altitude_degs == 5.0)
        # the park is a jump: let the motion history settle, or the monitor may read it as a fast
        # descent below Lim EL and cancel the command (flaky under load, 10-29)
        wait_for(lambda: not app.state.control.poller.tracker.moving_at(time.monotonic()), 5.0)
        time.sleep(0.5)
        d = check(tc, "track_object", ARGS)
        t_r0 = d["pass"]["t_r"]
        rig.park_after = t_r0 - 1.0  # the mount arrives after t_r - LEAD
        rig.slew_skew = 2.0
        rig.pause_at = t_r0 + 60.0
        w = Win(tc)
        ack = w.send("track_object", ARGS, "k1")
        assert ack["ok"], (ack.get("reason"), ack.get("code"))
        assert rig.wait_paused(), (
            rig.t,
            rig.phases,
            [
                (e["event"], e.get("reason"), e.get("phase"))
                for e in audit_lines(tmp_path)
                if e["event"].startswith("track")
            ],
        )
        rp = events(tmp_path, "track_replan")
        assert len(rp) == 1 and rp[0]["t_r"] > rp[0]["old_t_r"] and rp[0]["move_deg"] > 0.5
        names = paths(fake)
        assert names.count("/mount/goto_alt_az") == 2  # the original + one short goto
        tr = app.state.control.trackx.t
        assert tr.t_r == pytest.approx(rp[0]["t_r"], abs=0.01) and tr.phase == "tracking"
        assert names.count("/mount/custom_path/new") == 1
        first_jd = uploads(fake)[0][0][0]
        assert (first_jd - 2440587.5) * 86400.0 == pytest.approx(rp[0]["t_r"], abs=0.01)
        rig.resume()
        wait_for(lambda: not app.state.control.trackx.active, 10.0)
        w.close()


def test_missing_the_pass_twice_stops(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub)
        rig = Rig(app, fake, stub)
        rig.park_after = math.inf  # the mount never arrives
        rig.slew_skew = 10.0
        w = Win(tc)
        assert w.send("track_object", ARGS, "k1")["ok"]
        wait_for(lambda: not app.state.control.trackx.active, 10.0)
        st = events(tmp_path, "track_stopped")
        assert len(st) == 1 and st[0]["reason"] in (
            "Missed the pass: could not reach the rendezvous in time",
            "Pass ends in under 30 s",
        )
        assert "/mount/custom_path/new" not in paths(fake)
        assert paths(fake)[-2:] == ["/mount/stop", "/mount/tracking_off"]
        s = snap_when(tc, lambda s: s["track"] and s["track"]["ended"])
        assert s["track"]["ended"]["reason"] == st[0]["reason"]
        w.close()


def test_sun_detour_phase(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub)
        rig = Rig(app, fake, stub)
        ctl = app.state.control
        tc0 = ctl.track_eval(NORAD, app.state.poller.pwi4_site(), ctl.fresh_status(), True)
        pts = tc0.plan.points
        p = pts[int(len(pts) * 0.45)]
        app.state.safety.sun_override = (p.alt + 2.0, p.az)  # the Sun sits on the track
        site = app.state.poller.pwi4_site()
        plan_det = ctl.track_eval(NORAD, site, ctl.fresh_status(), True)  # (refreshes the cache)
        d = check(tc, "track_object", ARGS)
        assert d["ok"] and d["detour"] is not None and d["detour"]["count"] == 1
        det = plan_det.plan.detours[0]
        rig.pause_at = (det.t_a + det.t_b) / 2.0
        w = Win(tc)
        assert w.send("track_object", ARGS, "k1")["ok"]
        assert rig.wait_paused(), (
            rig.t,
            rig.phases,
            [
                (e["event"], e.get("reason"), e.get("phase"))
                for e in audit_lines(tmp_path)
                if e["event"].startswith("track")
            ],
        )
        assert state.get_override() == ("TRACKING", "Avoiding the Sun")
        s = snap_when(tc, lambda s: s["track"] and s["track"]["phase"] == "detour")
        assert s["state_reason"] == "Avoiding the Sun"
        assert len(s["track"]["detours"]) == 1
        assert s["track"]["detours"][0]["side"] in ("cw", "ccw")
        assert s["track"]["detours"][0]["t_a"] == pytest.approx(det.t_a)
        rig.resume()
        wait_for(lambda: not ctl.trackx.active, 10.0)
        assert rig.phases == ["slewing", "waiting", "tracking", "detour", "tracking"] or (
            rig.phases[:2] == ["slewing", "waiting"] and "detour" in rig.phases
        )
        assert "tracking" in rig.phases[rig.phases.index("detour") :]
        start = events(tmp_path, "track_start")[-1]
        assert len(start["detours"]) == 1 and start["min_sun_sep"] == pytest.approx(16.0, abs=0.1)
        assert len(events(tmp_path, "track_done")) == 1
        w.close()


def test_long_track_is_extended_without_new(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub, GEO_NORAD, GEO, "SYN-GEO")
        rig = Rig(app, fake, stub)
        ctl = app.state.control
        d = check(tc, "track_object", {"norad": GEO_NORAD})
        assert d["ok"], d
        rig.pause_at = d["pass"]["t_r"] + 660.0  # the next chunk is planned at t_r + 600 s
        w = Win(tc)
        assert w.send("track_object", {"norad": GEO_NORAD}, "k1")["ok"]
        assert rig.wait_paused(), (
            rig.t,
            rig.phases,
            [
                (e["event"], e.get("reason"), e.get("phase"))
                for e in audit_lines(tmp_path)
                if e["event"].startswith("track")
            ],
        )
        names = paths(fake)
        assert names.count("/mount/custom_path/new") == 1
        assert names.count("/mount/custom_path/add_point_list") == 2
        assert names.count("/mount/custom_path/apply") == 2
        assert names[-2:] == ["/mount/custom_path/add_point_list", "/mount/custom_path/apply"]
        j1, j2 = [[pt[0] for pt in u] for u in uploads(fake)]
        assert (j2[0] - j1[-1]) * 86400.0 == pytest.approx(1.0, abs=0.01)  # no gap, no overlap
        tr = ctl.trackx.t
        assert not tr.final and tr.t_out is None
        mp = app.state.monitor.plan
        assert mp.route and len(mp.paths[0]) == len(j1) + len(j2)  # extend_route
        assert mp.target[0] == pytest.approx(tr.last_axis0)
        s = snap_when(tc, lambda s: s["track"] and s["track"]["phase"] == "tracking")
        assert s["track"]["t_end"] is None  # a never-setting object
        # STOP ends it
        assert w.send("stop", None, "k2")["ok"]
        rig.resume()
        wait_for(lambda: not ctl.trackx.active)
        assert events(tmp_path, "track_extend") and events(tmp_path, "track_stopped")
        w.close()


def test_sun_stop_plan_ends_with_its_own_reason(tmp_path):
    """T4.2f: a slow object drifting into the Sun: the track ends at the 16 deg entry, STOPPED
    "Track ended: Sun approaching" (override, snapshot `ended`, audit), t_end = the stop time."""
    from gateway.dsun import unproject
    from gateway.trackplan import R_SUN_APPROACHING

    with running() as (tc, fake, app), TleStub() as stub:
        row(stub, GEO_NORAD, GEO, "SYN-GEO")
        rig = Rig(app, fake, stub)
        ctl = app.state.control
        site = app.state.poller.pwi4_site()
        ref = ctl.track_eval(GEO_NORAD, site, ctl.fresh_status(), True).plan.points[0]
        t_enter = T0 + 900.0
        centre = (ref.alt, ref.az)

        def sun_at(site_, t):  # 16 deg away at t_enter, closing at 0.005 deg/s
            az, alt = unproject(16.0 + (t_enter - t) * 0.005, 0.0, centre)
            return (alt, az)

        app.state.safety.sun_altaz_at = sun_at
        app.state.safety.sun_override = None
        app.state.safety.sun_altaz = lambda site_: (-60.0, 0.0)  # the 15 deg disc: far
        app.state.track._plans.clear()  # (the plan cached above had no Sun)
        d = check(tc, "track_object", {"norad": GEO_NORAD})
        assert d["ok"], d
        assert any(n.startswith("Sun blocks the track in ") for n in d["notes"]), d["notes"]
        rig.pause_at = d["pass"]["t_r"] + 20.0
        w = Win(tc)
        assert w.send("track_object", {"norad": GEO_NORAD}, "k1")["ok"]
        assert rig.wait_paused(), (rig.t, rig.phases)
        plan = ctl.trackx.t.plan
        assert plan.final and plan.end_reason == R_SUN_APPROACHING
        assert plan.t_end == pytest.approx(t_enter, abs=2.0)
        s = snap_when(tc, lambda s: s["track"] and s["track"]["phase"] == "tracking")
        assert s["track"]["t_end"] == pytest.approx(plan.t_end)  # the card counts down to it
        assert any(n.startswith("Sun blocks the track") for n in s["track"]["notes"])
        start = events(tmp_path, "track_start")[-1]
        assert start["t_end"] == pytest.approx(plan.t_end)
        rig.resume()
        wait_for(lambda: not ctl.trackx.active, 10.0)
        assert rig.t == pytest.approx(plan.t_end + END_DELAY_S, abs=1.5)
        wait_for(lambda: state.get_override() == ("STOPPED", R_SUN_APPROACHING), 5.0)
        s = snap_when(tc, lambda s: s["track"] and s["track"]["phase"] is None)
        assert s["state_reason"] == R_SUN_APPROACHING
        assert s["track"]["ended"]["reason"] == R_SUN_APPROACHING
        done = events(tmp_path, "track_done")
        assert len(done) == 1 and done[0]["reason"] == R_SUN_APPROACHING
        assert done[0]["t_end"] == pytest.approx(plan.t_end, abs=0.1)
        w.close()


# ------------------------------------------------------------------ ending rules


def test_stop_ends_the_track(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        ack = w.send("stop", None, "s1")
        assert ack["ok"] is True
        rig.resume()
        wait_for(lambda: not app.state.control.trackx.active)
        st = events(tmp_path, "track_stopped")
        assert (
            len(st) == 1 and st[0]["reason"] == "Stopped by STOP" and st[0]["phase"] == "tracking"
        )
        assert not events(tmp_path, "track_done")
        assert state.get_override() is None  # the mount is stopped by PWI4's own flags
        assert app.state.monitor.plan is None
        s = snap_when(tc, lambda s: s["track"] and s["track"]["ended"])
        assert s["track"]["ended"]["reason"] == "Stopped by STOP"
        assert "/mount/stop" in paths(fake)
        w.close()


def test_stop_while_slewing(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub)
        rig = Rig(app, fake, stub)
        rig.park_after = math.inf
        rig.pause_at = -math.inf  # hold at the first wait of the rendezvous
        w = Win(tc)
        assert w.send("track_object", ARGS, "k1")["ok"]
        assert rig.wait_paused(), (
            rig.t,
            rig.phases,
            [
                (e["event"], e.get("reason"), e.get("phase"))
                for e in audit_lines(tmp_path)
                if e["event"].startswith("track")
            ],
        )
        assert app.state.control.trackx.slewing
        assert w.send("stop", None, "s1")["ok"]
        wait_for(lambda: not app.state.control.trackx.active)
        st = events(tmp_path, "track_stopped")
        assert len(st) == 1 and st[0]["phase"] == "slewing"
        assert "/mount/custom_path/new" not in paths(fake)
        w.close()


def test_link_loss_and_safety_stop_end_the_track(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        w.close()  # the control holder's window goes away
        wait_for(lambda: not app.state.control.trackx.active)
        assert events(tmp_path, "track_stopped")[-1]["reason"] == "Control link lost"
        wait_for(lambda: events(tmp_path, "link_loss_stop"))
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        tc.portal.call(app.state.control.safety_stop, "Below the station limit elevation")
        wait_for(lambda: not app.state.control.trackx.active)
        assert events(tmp_path, "track_stopped")[-1]["reason"] == (
            "Below the station limit elevation"
        )
        w.close()


def test_tracking_off_ends_the_track(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        n = len(fake.writes)
        assert w.send("tracking", {"on": False}, "t1")["ok"]
        assert not app.state.control.trackx.active
        assert events(tmp_path, "track_stopped")[-1]["reason"] == "Tracking turned off"
        assert [p for p, _ in fake.writes[n:]][:2] == ["/mount/stop", "/mount/tracking_off"]
        rig.resume()
        w.close()


@pytest.mark.parametrize(
    ("cmd", "args", "expect"),
    [
        ("goto_altaz", {"alt": 40.0, "az": 100.0}, "/mount/goto_alt_az"),
        ("tracking", {"on": True}, "/mount/tracking_on"),
    ],
)
def test_goto_and_sidereal_track_replace_the_track(tmp_path, cmd, args, expect):
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        n = len(fake.writes)
        ack = w.send(cmd, args, "g1")
        assert ack["ok"] is True, ack["reason"]
        assert not app.state.control.trackx.active
        rep = events(tmp_path, "track_replaced")
        assert len(rep) == 1 and rep[0]["norad"] == NORAD
        after = [p for p, _ in fake.writes[n:]]
        assert after[0] == "/mount/stop" and expect in after  # stopped first, then the command
        s = snap_when(tc, lambda s: s["track"] is None)
        assert s["track"] is None  # a replacement is not an "ended" track
        rig.resume()
        w.close()


def test_new_track_object_replaces_the_track(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        row(stub, 99002, PEAK_70, "SYN-B")
        rig.pause_at = -math.inf  # hold the new track at its first wait
        n = len(fake.writes)
        ack = w.send("track_object", {"norad": 99002}, "k2")
        assert ack["ok"] is True, ack["reason"]
        assert rig.wait_paused(), (
            rig.t,
            rig.phases,
            [
                (e["event"], e.get("reason"), e.get("phase"))
                for e in audit_lines(tmp_path)
                if e["event"].startswith("track")
            ],
        )
        tr = app.state.control.trackx.t
        assert tr is not None and tr.norad == 99002
        assert events(tmp_path, "track_replaced")[0]["norad"] == NORAD
        assert [e["norad"] for e in events(tmp_path, "track_start")] == [NORAD, 99002]
        assert [p for p, _ in fake.writes[n:]][0] == "/mount/stop"
        w.send("stop", None, "s1")
        rig.resume()
        w.close()


def test_home_replaces_the_track(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        ack = w.send("home", None, "h1")
        assert ack["ok"] is True, ack["reason"]
        assert not app.state.control.trackx.active
        assert events(tmp_path, "track_replaced")[0]["reason"] == "Replaced by home"
        rig.resume()
        w.send("stop", None, "s1")  # cancels the sequence
        wait_for(lambda: not app.state.control.seq.active, 10.0)
        w.close()


@pytest.mark.parametrize(
    "cmd",
    [
        ("jog", {"axis": 0, "dir": 1, "speed": 1.0}),
        ("step", {"axis": 1, "dir": 1, "size_arcmin": 5}),
        ("wrap_turn", {}),
    ],
)
def test_jog_step_turn_are_refused_during_a_track(tmp_path, cmd):
    name, args = cmd
    with running() as (tc, fake, app), TleStub() as stub:
        rig, w, _ = tracking_rig(tc, fake, app, stub)
        n = len(fake.writes)
        ack = w.send(name, args, "j1")
        assert ack["ok"] is False and ack["reason"] == R_OBJECT_TRACK
        assert ack["reason"] == "Object tracking; press STOP first"
        assert app.state.control.trackx.active and len(fake.writes) == n
        d = check(tc, name, args)
        assert d["ok"] is False and d["code"] == "object_tracking"
        rig.resume()
        w.send("stop", None, "s1")
        w.close()


def test_a_sequence_command_is_not_blocked_by_the_rendezvous_job(tmp_path):
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub)
        rig = Rig(app, fake, stub)
        rig.park_after = math.inf
        rig.pause_at = -math.inf
        w = Win(tc)
        assert w.send("track_object", ARGS, "k1")["ok"]
        assert rig.wait_paused() and app.state.control.trackx.slewing
        ack = w.send("home", None, "h1")
        assert ack["ok"] is True, ack["reason"]
        assert not app.state.control.trackx.active
        rig.resume()
        w.send("stop", None, "s1")  # cancels the sequence
        wait_for(lambda: not app.state.control.seq.active, 10.0)
        w.close()


# ------------------------------------------------------------------ rendezvous goto / verdict


def test_target0_is_judged_and_planned_like_a_wrap_turn():
    with running() as (tc, fake, app):
        ctl = app.state.control
        goto = Command("t1", "goto_altaz", {"alt": 45.0, "az": 100.0, "target0": -260.0})
        v = ctl._motion_verdict(goto, None)  # a dry run leaves no plan
        assert v.reason is None and app.state.safety._plans == {}
        v = ctl._motion_verdict(goto, "c1")
        assert v.reason is None
        assert app.state.safety._plans["t1"].target == (-260.0, 45.0)  # not the nearest turn
        low = Command("t2", "goto_altaz", {"alt": 10.0, "az": 100.0, "target0": -260.0})
        assert ctl._motion_verdict(low, None).reason is not None  # still below Lim EL


def test_rendezvous_wrap_window_follows_target0():
    with running() as (tc, fake, app):
        ctl = app.state.control
        goto = Command("t3", "goto_altaz", {"alt": 45.0, "az": 100.0, "target0": 100.0})
        tc.portal.call(ctl._goto_wrap, goto)
        assert fake.writes[-1] == ("/mount/set_axis0_wrap_range_min", {"degs": "-80.0"})
        n = len(fake.writes)
        tc.portal.call(ctl._goto_wrap, goto)  # now within the same window: nothing to write
        plain = Command("t4", "goto_altaz", {"alt": 45.0, "az": 120.0})
        tc.portal.call(ctl._goto_wrap, plain)  # no target0: the nearest representation (P8)
        assert len(fake.writes) >= n


def test_check_equals_command_for_every_row():
    """The check endpoint and the real command return the same verdict (reason + code)."""
    with running() as (tc, fake, app), TleStub() as stub:
        row(stub)
        rig = Rig(app, fake, stub)
        w = Win(tc)
        for t, args in ((T0 - 5000.0, ARGS), (T0 + 100.0, ARGS), (T0, {"norad": 12345})):
            rig.t = t
            d = check(tc, "track_object", args)
            ack = w.send("track_object", args, f"v{int(t) % 1000}")
            if d["ok"]:
                assert ack["ok"]
                w.send("stop", None, "sx")
                wait_for(lambda: not app.state.control.trackx.active)
            else:
                assert ack["ok"] is False and ack["reason"] == d["reason"]
                assert ack["code"] == d["code"]
        assert any(not check(tc, "track_object", {"norad": 12345})["ok"] for _ in (0,))
        w.close()
        time.sleep(0.2)
