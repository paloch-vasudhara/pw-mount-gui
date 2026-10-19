"""Generate synthetic TLEs (LEO/MEO/GEO mix) and benchmark python-sgp4 + skyfield az/el."""
import json, math, sys, time
import numpy as np
from sgp4.api import Satrec, SatrecArray, WGS72, jday
from sgp4.exporter import export_tle

out_dir = sys.argv[1]; N = int(sys.argv[2])
rng = np.random.default_rng(42)
epoch = (2026, 10, 19, 12, 0, 0.0)
jd0, fr0 = jday(*epoch)
epoch_days = jd0 + fr0 - 2433281.5                     # days since 1949-12-31 00:00 UT
tles = []
for i in range(N):
    r = rng.random()
    if r < 0.80:   n_rev = rng.uniform(12.5, 16.2); ecc = rng.uniform(0, 0.02); inc = rng.uniform(0, 100)
    elif r < 0.90: n_rev = rng.uniform(1.8, 2.3);   ecc = rng.uniform(0, 0.02); inc = rng.uniform(50, 65)
    else:          n_rev = rng.uniform(0.99, 1.01); ecc = rng.uniform(0, 0.001); inc = rng.uniform(0, 5)
    s = Satrec()
    s.sgp4init(WGS72, 'i', 10000 + i, epoch_days, 1e-5, 0.0, 0.0, ecc,
               math.radians(rng.uniform(0, 360)), math.radians(inc), math.radians(rng.uniform(0, 360)),
               n_rev * 2 * math.pi / 1440.0, math.radians(rng.uniform(0, 360)))
    l1, l2 = export_tle(s)
    tles.append([l1, l2])
json.dump(tles, open(f"{out_dir}/tles.json", "w"))

# Time grid: 30 min at 10 s (181 samples) — one sky-viewer chunk.
steps = np.arange(0, 1801, 10.0)
jd = np.full(steps.shape, jd0); fr = fr0 + steps / 86400.0

t = time.perf_counter()
arr = SatrecArray([Satrec.twoline2rv(a, b) for a, b in tles])
t_parse = time.perf_counter() - t
t = time.perf_counter()
e, r, v = arr.sgp4(jd, fr)                              # (N, T, 3) TEME km
t_prop = time.perf_counter() - t

# Topocentric az/el with skyfield (TEME -> ITRS incl. polar-motion-free GMST rotation), vectorised.
from skyfield.api import load, wgs84
from skyfield.sgp4lib import theta_GMST1982
ts = load.timescale()
site = wgs84.latlon(13.7862, 101.4639, elevation_m=40.8)
t = time.perf_counter()
jd_full = jd + fr
# rotate each time slice (vectorised over satellites)
lat, lon = math.radians(13.7862), math.radians(101.4639)
obs = site.itrs_xyz.km
R = np.array([[-math.sin(lat)*math.cos(lon), -math.sin(lat)*math.sin(lon), math.cos(lat)],
              [-math.sin(lon), math.cos(lon), 0.0],
              [math.cos(lat)*math.cos(lon), math.cos(lat)*math.sin(lon), math.sin(lat)]])  # ECEF->SEZ-ish (N,E,U rows below)
az = np.empty(r.shape[:2]); el = np.empty(r.shape[:2])
for k in range(len(steps)):
    th, _ = theta_GMST1982(jd[k], fr[k])
    c, s_ = math.cos(th), math.sin(th)
    rk = r[:, k, :]
    rI = np.stack([c*rk[:, 0] + s_*rk[:, 1], -s_*rk[:, 0] + c*rk[:, 1], rk[:, 2]], -1)
    d = rI - obs
    north = d @ R[0]; east = d @ R[1]; up = d @ R[2]
    az[:, k] = (np.degrees(np.arctan2(east, north)) + 360) % 360
    el[:, k] = np.degrees(np.arctan2(up, np.hypot(north, east)))
t_azel = time.perf_counter() - t
json.dump({"az": az[:50, ::30].tolist(), "el": el[:50, ::30].tolist(), "teme": r[:50, ::30, :].tolist()},
          open(f"{out_dir}/sgp4_sample.json", "w"))
print(json.dumps({"N": N, "samples_per_sat": len(steps), "parse_s": round(t_parse, 3),
                  "sgp4_prop_s": round(t_prop, 3), "azel_s": round(t_azel, 3),
                  "total_s": round(t_parse + t_prop + t_azel, 3), "errors": int((e != 0).sum())}))
