"""T4.1a: pass maths (vs a skyfield brute force) and the TRACK OBJECT rule table.

The TLEs are representative, not live: classical elements for an ISS-like LEO, a GPS-like MEO,
a GEO and a Molniya-like HEO at the epoch 2026-10-28 12:00 UTC (checksums valid, GEO longitude
tuned to ~104 E). Everything is evaluated at fixed Unix times near that epoch, never the wall
clock.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from skyfield.api import EarthSatellite, load, wgs84

from gateway import passes
from gateway.passes import PassInfo, compute_pass, refresh_now, track_verdict

TS = load.timescale()
SITE = (14.3479, 100.5656, 12.0)
TOPOS = wgs84.latlon(SITE[0], SITE[1], elevation_m=SITE[2])
T0 = 1793188800.0  # 2026-10-28 12:00:00 UTC, the epoch of the TLEs
LIM = 15.0

LEO = (
    "1 25544U 98067A   26301.50000000  .00000000  00000-0  00000-0 0  9997",
    "2 25544  51.6400 120.0000 0006000  80.0000  40.0000 15.50000000100001",
)
MEO = (
    "1 40105U 14045A   26301.50000000  .00000000  00000-0  00000-0 0  9991",
    "2 40105  55.0000 200.0000 0100000  30.0000 150.0000  2.00570000100009",
)
GEO = (
    "1 41838U 16071A   26301.50000000  .00000000  00000-0  00000-0 0  9996",
    "2 41838   0.0500  80.0000 0002000 150.0000  90.8636  1.00270000100000",
)


def sun_down():
    return (-60.0, 0.0)


def info_at(tle, t, lim=LIM):
    return compute_pass(*tle, SITE, lim, t, sun_down, TS)


def brute_alt(tle, t: np.ndarray) -> np.ndarray:
    """Independent reference: skyfield, geometric, straight from the unix times."""
    sat = EarthSatellite(*tle, ts=TS)
    alt, _, _ = (sat - TOPOS).at(passes._times(TS, t)).altaz()
    return alt.degrees


def brute_crossings(tle, t0, t1, step, level):
    t = np.arange(t0, t1, step)
    d = brute_alt(tle, t) - level
    out = {"up": [], "down": []}
    for j in np.nonzero(d[:-1] * d[1:] < 0)[0]:
        x = t[j] + step * (-d[j] / (d[j + 1] - d[j]))
        out["up" if d[j + 1] > d[j] else "down"].append(float(x))
    return out, t, d + level


# ------------------------------------------------------------------ maths vs brute force


def test_unix_seconds_match_datetime_conversion():
    from datetime import UTC, datetime

    t = T0 + 12345.678
    a = passes._times(TS, np.array([t]))
    b = TS.from_datetime(datetime.fromtimestamp(t, UTC))
    assert abs(a.tt[0] - b.tt) * 86400 < 1e-3


def test_coarse_pipeline_agrees_with_skyfield():
    sat = EarthSatellite(*LEO, ts=TS)
    t = T0 + 37000 + np.arange(0, 1500.0, 7.0)
    fast = passes._coarse_alt(sat, TOPOS, TS, t)
    exact = brute_alt(LEO, t)
    assert float(np.max(np.abs(fast - exact))) < 0.01  # the fine grid pads for this


def test_leo_pass_vs_dense_brute_force():
    # rises ~T0+78330 s with a 62 deg peak; "now" is mid-pass
    info = info_at(LEO, T0 + 78500)
    assert info.alt > 0 and info.sun_sep == pytest.approx(angsep_sun(info), abs=1e-6)
    ref, t, alt = brute_crossings(LEO, info.t_rise - 30, info.t_set + 30, 0.1, 0.0)
    ref_lim, _, _ = brute_crossings(LEO, info.t_rise - 30, info.t_set + 30, 0.1, LIM)
    assert len(ref["up"]) == len(ref["down"]) == 1
    assert info.t_rise == pytest.approx(ref["up"][0], abs=0.2)
    assert info.t_set == pytest.approx(ref["down"][0], abs=0.2)
    assert info.t_in == pytest.approx(ref_lim["up"][0], abs=0.2)
    assert info.t_out == pytest.approx(ref_lim["down"][0], abs=0.2)
    assert info.peak_alt == pytest.approx(float(alt.max()), abs=0.001)
    assert abs(info.t_peak - float(t[int(np.argmax(alt))])) < 3.0  # flat top: loose in time
    assert info.t_rise < info.t_in < info.t_peak < info.t_out < info.t_set


def test_leo_low_pass_peaks_below_lim_el():
    info = info_at(LEO, T0 + 37100)  # rises ~T0+37060, peak 9.9 deg
    assert info.alt >= 0
    ref, t, alt = brute_crossings(LEO, info.t_rise - 30, info.t_set + 30, 0.1, 0.0)
    assert info.t_rise == pytest.approx(ref["up"][0], abs=0.2)
    assert info.t_set == pytest.approx(ref["down"][0], abs=0.2)
    assert info.peak_alt == pytest.approx(float(alt.max()), abs=0.001)
    assert info.peak_alt < LIM
    assert info.t_in is None and info.t_out is None


def test_meo_pass():
    info = info_at(MEO, T0 + 60000)  # above Lim EL, rising, ~8 h pass
    ref0, _, _ = brute_crossings(MEO, T0 + 50000, T0 + 100000, 1.0, 0.0)
    ref, t, alt = brute_crossings(MEO, T0 + 50000, T0 + 100000, 1.0, LIM)
    assert info.t_rise == pytest.approx(ref0["up"][0], abs=0.2)
    assert info.t_set == pytest.approx(ref0["down"][0], abs=0.2)
    assert info.t_in == pytest.approx(ref["up"][0], abs=0.2)
    assert info.t_out == pytest.approx(ref["down"][0], abs=0.2)
    window = (t >= info.t_rise) & (t <= info.t_set)
    assert info.peak_alt == pytest.approx(float(alt[window].max()), abs=0.001)
    assert info.t_set - info.t_rise > 3 * 3600


def test_geo_never_sets_and_lim_el_crossings_inside_window():
    info = info_at(GEO, T0)
    assert info.t_rise is None and info.t_set is None
    assert info.t_in is None and info.t_out is None  # 72.7 deg all day, Lim EL 15
    t = np.arange(T0, T0 + 43200.0, 1.0)
    alt = brute_alt(GEO, t)
    assert info.peak_alt == pytest.approx(float(alt.max()), abs=0.001)
    # a Lim EL inside the daily wobble, window centred on the peak: both crossings are found
    t1 = T0 + 30000
    t = np.arange(t1, t1 + 43200.0, 1.0)
    alt = brute_alt(GEO, t)
    lim = float(alt.max() - 0.5 * (alt.max() - min(alt[0], alt[-1])))
    info = info_at(GEO, t1, lim)
    ref, _, _ = brute_crossings(GEO, t1, t1 + 43200, 1.0, lim)
    assert len(ref["up"]) == len(ref["down"]) == 1
    assert info.t_in == pytest.approx(ref["up"][0], abs=0.2)
    assert info.t_out == pytest.approx(ref["down"][0], abs=0.2)
    # forward window ends before the descent: only the up crossing, t_out None
    info = info_at(GEO, T0, float((alt.max() + 72.69) / 2))
    assert info.t_in is not None and info.t_out is None


def test_below_horizon_returns_live_fields_only():
    info = info_at(LEO, T0)  # alt -28.6 deg
    assert info.alt < 0
    assert info.peak_alt is None and info.t_rise is None and info.t_set is None
    assert track_verdict(info, T0, LIM) == ("Below the horizon", "below_horizon", None)


def test_sun_separation_uses_sun_fn_and_none_when_unavailable():
    info = compute_pass(*GEO, SITE, LIM, T0, lambda: (info_alt(GEO), 0.0), TS)
    assert info.sun_sep is not None and info.sun_sep < 180
    assert compute_pass(*GEO, SITE, LIM, T0, lambda: None, TS).sun_sep is None


def test_refresh_now_keeps_the_pass_and_moves_the_live_fields():
    info = info_at(LEO, T0 + 78500)
    later = refresh_now(info, *LEO, SITE, T0 + 78530, lambda: (-60.0, 0.0), TS)
    assert (later.t_rise, later.t_in, later.t_peak, later.t_out, later.t_set) == (
        info.t_rise,
        info.t_in,
        info.t_peak,
        info.t_out,
        info.t_set,
    )
    assert later.alt != info.alt and later.az != info.az
    assert later.alt == pytest.approx(float(brute_alt(LEO, np.array([T0 + 78530]))[0]), abs=1e-4)


def test_decayed_elements_raise():
    bad = (LEO[0], LEO[1].replace("0006000", "9900000"))  # perigee far below the surface
    with pytest.raises(ValueError):
        compute_pass(*bad, SITE, LIM, T0 + 78500, sun_down, TS)


@pytest.mark.parametrize(
    ("name", "tle", "t"), [("LEO", LEO, T0 + 78500), ("MEO", MEO, T0 + 60000), ("GEO", GEO, T0)]
)
def test_compute_pass_budget_30ms(name, tle, t, capsys):
    info_at(tle, t)  # warm up
    best = min(_timed(tle, t) for _ in range(7))
    with capsys.disabled():
        print(f"\n  compute_pass {name}: {best:.1f} ms (best of 7)")
    assert best < 30.0


def _timed(tle, t) -> float:
    s = time.perf_counter()
    info_at(tle, t)
    return (time.perf_counter() - s) * 1000.0


def info_alt(tle) -> float:
    return float(brute_alt(tle, np.array([T0]))[0])


def angsep_sun(info: PassInfo) -> float:
    from gateway.ephem import angular_sep_deg

    return angular_sep_deg(info.az, info.alt, 0.0, -60.0)


# ------------------------------------------------------------------ the rule table

NOW = 1000.0


def pinfo(**kw) -> PassInfo:
    """A synthetic mid-pass: alt 30 (>= Lim EL 15), rising, 200 s left above Lim EL."""
    base = dict(
        alt=30.0,
        az=100.0,
        rising=True,
        sun_sep=90.0,
        t_rise=NOW - 300,
        t_in=NOW - 100,
        t_peak=NOW + 50,
        peak_alt=60.0,
        t_out=NOW + 200,
        t_set=NOW + 400,
    )
    base.update(kw)
    return PassInfo(**base)


def test_row2_below_horizon():
    assert track_verdict(pinfo(alt=-0.01), NOW, LIM) == (
        "Below the horizon",
        "below_horizon",
        None,
    )
    assert track_verdict(pinfo(alt=0.0), NOW, LIM)[0] != "Below the horizon"  # alt >= 0 passes


def test_row3_sun_within_15_deg():
    r = track_verdict(pinfo(sun_sep=14.99), NOW, LIM)
    assert r == ("Within 15° of the Sun", "sun", None)
    assert track_verdict(pinfo(sun_sep=15.0), NOW, LIM)[0] is None  # exactly 15: allowed
    assert track_verdict(pinfo(sun_sep=None), NOW, LIM)[0] is None  # no Sun available


def test_row4_pass_never_reaches_lim_el():
    r = track_verdict(pinfo(alt=10.0, peak_alt=14.99, t_in=None, t_out=None), NOW, LIM)
    assert r == ("Pass peaks below the station limit elevation", "peak_below_limit", None)
    # peak exactly at Lim EL is enough
    assert track_verdict(pinfo(peak_alt=LIM), NOW, LIM)[0] is None


def test_row5_setting_below_lim_el():
    # below Lim EL after the peak
    r = track_verdict(pinfo(alt=10.0, rising=False, t_peak=NOW - 60), NOW, LIM)
    assert r == ("Setting below the station limit elevation", "setting", None)
    # same altitude but still rising before the peak: not "setting"
    assert track_verdict(pinfo(alt=10.0, t_peak=NOW + 60, t_in=NOW + 20), NOW, LIM)[0] is None
    # a never-setting object falling below Lim EL with the forward peak ahead is also "setting"
    r = track_verdict(pinfo(alt=10.0, rising=False, t_peak=NOW + 600), NOW, LIM)
    assert r[1] == "setting"
    # above Lim EL after the peak is fine (row 6 decides)
    assert track_verdict(pinfo(alt=20.0, rising=False, t_peak=NOW - 60), NOW, LIM)[0] is None


def test_row6_pass_ends_in_under_30_s():
    r = track_verdict(pinfo(t_out=NOW + 29.99), NOW, LIM)
    assert r == ("Pass ends in under 30 s", "pass_too_short", None)
    assert track_verdict(pinfo(t_out=NOW + 30.0), NOW, LIM)[0] is None  # exactly 30 s left
    # before t_in the time left counts from t_in, not from now
    r = track_verdict(pinfo(alt=10.0, t_in=NOW + 60, t_out=NOW + 89, t_peak=NOW + 75), NOW, LIM)
    assert r[1] == "pass_too_short"
    r = track_verdict(pinfo(alt=10.0, t_in=NOW + 60, t_out=NOW + 90, t_peak=NOW + 75), NOW, LIM)
    assert r[0] is None
    # an already-passed t_in does not extend the time left
    assert track_verdict(pinfo(t_in=NOW - 1000, t_out=NOW + 20), NOW, LIM)[1] == "pass_too_short"


def test_row6_t_out_none_means_unbounded():
    assert track_verdict(pinfo(t_out=None, t_set=None), NOW, LIM) == (None, None, "track")
    assert track_verdict(pinfo(t_out=None, t_in=None, t_rise=None), NOW, LIM)[0] is None


def test_ok_phase_track_vs_pre_position():
    assert track_verdict(pinfo(), NOW, LIM) == (None, None, "track")
    assert track_verdict(pinfo(alt=LIM), NOW, LIM) == (None, None, "track")  # at Lim EL: track
    pre = pinfo(alt=10.0, t_in=NOW + 40, t_peak=NOW + 100)
    assert track_verdict(pre, NOW, LIM) == (None, None, "pre_position")


def test_first_failing_row_wins():
    # below horizon beats the Sun; the Sun beats the short pass
    assert track_verdict(pinfo(alt=-1.0, sun_sep=5.0), NOW, LIM)[1] == "below_horizon"
    assert track_verdict(pinfo(sun_sep=5.0, peak_alt=5.0, t_out=NOW + 1), NOW, LIM)[1] == "sun"
    # peak below the limit beats setting and the short pass
    r = track_verdict(pinfo(alt=10.0, peak_alt=12.0, rising=False, t_out=NOW + 1), NOW, LIM)
    assert r[1] == "peak_below_limit"
    assert math.isfinite(NOW)
