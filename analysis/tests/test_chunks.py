from __future__ import annotations

import math

import numpy as np
import pytest
from skyfield.api import EarthSatellite, load, wgs84

from analysis.catalog import load_layers
from analysis.chunks import (
    STEP_S,
    Site,
    compute_chunk,
    compute_dense,
    decode_chunk,
    iter_tracks,
    n_steps_for,
    snap_t0,
    trim_mask,
)
from analysis.tests.fixture_catalog import ROWS, build_sqlite, sqlite_source, tle_of

SITE = Site(14.3479, 100.5656, 12.0)
DAY0 = 1792540800  # 2026-10-21 00:00:00 UTC
TS = load.timescale()
TOL = {"leo": 0.02, "meo": 0.01, "geo": 0.01}


@pytest.fixture(scope="module")
def layers(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("cat") / "cat.db")
    build_sqlite(path)
    return load_layers(sqlite_source(path), with_satrecs=True)


def _sky_truth(norad: int, unix: np.ndarray, site: Site = SITE):
    name = next(r[1] for r in ROWS if r[0] == norad)
    sat = EarthSatellite(*tle_of(norad), name, TS)
    t = TS.utc(2026, 10, 21, 0, 0, unix.astype(float) - DAY0)
    alt, az, _ = (sat - wgs84.latlon(site.lat, site.lon, site.alt_m)).at(t).altaz()
    return az.degrees, alt.degrees


def _sep_deg(az1, alt1, az2, alt2):
    a1, e1, a2, e2 = (np.radians(np.asarray(x, dtype=np.float64)) for x in (az1, alt1, az2, alt2))
    c = np.sin(e1) * np.sin(e2) + np.cos(e1) * np.cos(e2) * np.cos(a1 - a2)
    return np.degrees(np.arccos(np.clip(c, -1, 1)))


def _window_with_data(layers, layer, dur=1800, want=None):
    """First 10-minute-stepped t0 within the day where the layer has samples above 5 deg."""
    for k in range(0, 24 * 6):
        t0 = DAY0 + k * 600
        d = decode_chunk(compute_chunk(layers[layer], SITE, t0, dur, name=layer))
        if d["header"]["n_samples"] and (want is None or want in d["header"]["norad"]):
            if (d["samples"][:, 1] > 5).any():
                return t0, d
    raise AssertionError(f"no visible data for {layer}")


@pytest.mark.parametrize("layer", ["leo", "meo", "geo"])
def test_accuracy_vs_skyfield(layers, layer):
    # LEO: ISS-like and the SSO separately; both must be exercised.
    wants = {"leo": [25544, 43013], "meo": [24876], "geo": [41000, 41001, 39000]}[layer]
    for norad in wants:
        t0, d = _window_with_data(layers, layer, want=norad)
        tracks = [(s, smp) for n, s, smp in iter_tracks(d) if n == norad]
        assert tracks
        h = d["header"]
        worst, count = 0.0, 0
        for start, smp in tracks:
            unix = h["t0_unix"] + (start + np.arange(len(smp))) * h["step_s"]
            idx = np.unique(np.linspace(0, len(smp) - 1, 5).astype(int))  # >= 5 instants/track
            az, alt = _sky_truth(norad, unix[idx])
            sep = _sep_deg(smp[idx, 0], smp[idx, 1], az, alt)
            worst, count = max(worst, float(sep.max())), count + len(idx)
        assert count >= 1
        assert worst <= TOL[layer], f"{layer} {norad}: {worst} deg"


def test_five_instants_every_layer_all_samples(layers):
    for layer, norad in [("leo", 25544), ("meo", 24876), ("geo", 39000)]:
        t0, d = _window_with_data(layers, layer, want=norad)
        for n, start, smp in iter_tracks(d):
            if n != norad:
                continue
            unix = d["header"]["t0_unix"] + (start + np.arange(len(smp))) * d["header"]["step_s"]
            az, alt = _sky_truth(norad, unix)
            assert _sep_deg(smp[:, 0], smp[:, 1], az, alt).max() <= TOL[layer]


def test_roundtrip_and_alignment(layers):
    _, _ = _window_with_data(layers, "meo")
    buf = compute_chunk(layers["leo"], SITE, DAY0 + 4000, 1800, name="leo")
    (hlen,) = np.frombuffer(buf[:4], dtype="<u4")
    assert (4 + int(hlen)) % 4 == 0
    d = decode_chunk(buf)
    h = d["header"]
    assert h["layer"] == "leo" and h["step_s"] == 10 and h["n_steps"] == 181
    assert h["t0_unix"] == snap_t0(DAY0 + 4000, 10)
    assert d["segments"].shape == (h["n_seg"], 3) and d["samples"].shape == (h["n_samples"], 2)
    assert d["segments"][:, 2].sum() == h["n_samples"]
    assert d["segments"].dtype == np.dtype("<i4") and d["samples"].dtype == np.dtype("<f4")
    assert (d["segments"][:, 0] < len(h["norad"])).all()
    assert ((d["segments"][:, 1] + d["segments"][:, 2]) <= h["n_steps"]).all()
    assert (d["samples"][:, 0] >= 0).all() and (d["samples"][:, 0] < 360).all()
    assert len(buf) == 4 + hlen + 12 * h["n_seg"] + 8 * h["n_samples"]


def test_snapping_and_steps():
    assert snap_t0(DAY0 + 299, 300) == DAY0 and snap_t0(DAY0 + 305, 300) == DAY0 + 300
    assert n_steps_for(1800, 10) == 181 and n_steps_for(120, 10) == 13
    assert n_steps_for(1800, 300) == 7 and n_steps_for(1800, 60) == 31
    assert STEP_S == {"leo": 10, "meo": 60, "geo": 300}


@pytest.mark.parametrize("layer", ["leo", "meo", "geo"])
def test_trimming_at_the_rim(layers, layer):
    step = STEP_S[layer]
    t0_any = None
    for k in range(0, 24 * 6):
        t0 = DAY0 + k * 600
        norad, ok, az, alt = compute_dense(layers[layer], SITE, t0, step, n_steps_for(1800, step))
        keep = trim_mask(ok, alt)
        d = decode_chunk(compute_chunk(layers[layer], SITE, t0, 1800, name=layer))
        # Rebuild the keep matrix from the segments and compare with the reference rule.
        got = np.zeros_like(keep)
        order = {n: i for i, n in enumerate(norad)}
        for s_obj, s_start, s_n in d["segments"]:
            got[order[d["header"]["norad"][s_obj]], s_start : s_start + s_n] = True
        assert (got == keep).all()
        # Margin: every kept sample either is above -2 or touches an above-horizon neighbour.
        above = ok & (alt > -2.0)
        nb = np.zeros_like(above)
        nb[:, 1:] |= above[:, :-1]
        nb[:, :-1] |= above[:, 1:]
        assert (~keep | above | nb).all()
        # No above-horizon valid sample is ever lost.
        assert (~above | keep).all()
        if keep.any() and (keep & ~above).any():
            t0_any = t0
    assert t0_any is not None, "never exercised a rim sample"


def test_decayed_and_nonpayload_never_appear(layers):
    forbidden = {11111, 22222, 33333}
    for layer, data in layers.items():
        assert not forbidden & set(int(x) for x in data.norad)
        assert not forbidden & set(o[0] for o in data.objects())
        for k in range(0, 24 * 6, 3):
            d = decode_chunk(compute_chunk(data, SITE, DAY0 + k * 600, 1800, name=layer))
            assert not forbidden & set(d["header"]["norad"])
    assert set(int(x) for x in layers["leo"].norad) == {25544, 43013}
    assert set(int(x) for x in layers["meo"].norad) == {24876}
    assert set(int(x) for x in layers["geo"].norad) == {41000, 41001, 39000}  # GEO + HEO


def test_invalid_sgp4_samples_dropped(layers):
    # Far from the TLE epoch the LEO drag-free orbit stays valid; a ~decade out is still finite.
    # A HEO propagated absurdly far must never emit NaN samples.
    d = decode_chunk(compute_chunk(layers["geo"], SITE, DAY0 + 86400 * 4000, 1800, name="geo"))
    assert np.isfinite(d["samples"]).all()
    assert math.isfinite(float(d["header"]["t0_unix"]))
