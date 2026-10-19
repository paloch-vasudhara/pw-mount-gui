"""Benchmark Orekit TLEPropagator on the same TLEs/time grid; topocentric az/el via TopocentricFrame."""
import json, sys, time
out_dir, N, orekit_data = sys.argv[1], int(sys.argv[2]), sys.argv[3]
import orekit; vm = orekit.initVM()
from orekit.pyhelpers import setup_orekit_curdir; setup_orekit_curdir(orekit_data)
from org.orekit.propagation.analytical.tle import TLE, TLEPropagator
from org.orekit.time import AbsoluteDate, TimeScalesFactory
from org.orekit.frames import FramesFactory, TopocentricFrame
from org.orekit.bodies import OneAxisEllipsoid, GeodeticPoint
from org.orekit.utils import Constants, IERSConventions
import math
tles = json.load(open(f"{out_dir}/tles.json"))[:N]
utc = TimeScalesFactory.getUTC()
t0 = AbsoluteDate(2026, 10, 19, 12, 0, 0.0, utc)
itrf = FramesFactory.getITRF(IERSConventions.IERS_2010, True)
earth = OneAxisEllipsoid(Constants.WGS84_EARTH_EQUATORIAL_RADIUS, Constants.WGS84_EARTH_FLATTENING, itrf)
topo = TopocentricFrame(earth, GeodeticPoint(math.radians(13.7862), math.radians(101.4639), 40.8), "site")
teme = FramesFactory.getTEME()
dates = [t0.shiftedBy(float(s)) for s in range(0, 1801, 10)]
t = time.perf_counter()
props = [TLEPropagator.selectExtrapolator(TLE(a, b)) for a, b in tles]
t_parse = time.perf_counter() - t
t = time.perf_counter()
az_s, el_s, teme_s = [], [], []
for i, p in enumerate(props):
    row_az, row_el, row_r = [], [], []
    for k, d in enumerate(dates):
        pv = p.propagate(d).getPVCoordinates(teme)
        if i < 50 and k % 30 == 0:
            pos = pv.getPosition()
            row_r.append([pos.getX()/1000, pos.getY()/1000, pos.getZ()/1000])
        pt = pv.getPosition()
        el = topo.getElevation(pt, teme, d); az = topo.getAzimuth(pt, teme, d)
        if i < 50 and k % 30 == 0:
            row_az.append(math.degrees(az)); row_el.append(math.degrees(el))
    if i < 50: az_s.append(row_az); el_s.append(row_el); teme_s.append(row_r)
t_prop = time.perf_counter() - t
json.dump({"az": az_s, "el": el_s, "teme": teme_s}, open(f"{out_dir}/orekit_sample.json", "w"))
print(json.dumps({"N": N, "samples_per_sat": len(dates), "parse_s": round(t_parse, 3),
                  "prop_plus_azel_s": round(t_prop, 3), "total_s": round(t_parse + t_prop, 3)}))
