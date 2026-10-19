import json, sys, numpy as np
d = sys.argv[1]
a = json.load(open(f"{d}/sgp4_sample.json")); b = json.load(open(f"{d}/orekit_sample.json"))
ra, rb = np.array(a["teme"]), np.array(b["teme"])
dr = np.linalg.norm(ra - rb, axis=-1)
ea, eb = np.array(a["el"]), np.array(b["el"]); aa, ab = np.array(a["az"]), np.array(b["az"])
# great-circle separation of az/el directions
def vec(az, el):
    az, el = np.radians(az), np.radians(el)
    return np.stack([np.cos(el)*np.sin(az), np.cos(el)*np.cos(az), np.sin(el)], -1)
sep = np.degrees(np.arccos(np.clip((vec(aa, ea) * vec(ab, eb)).sum(-1), -1, 1)))
print(json.dumps({"teme_pos_diff_km_max": float(dr.max()), "teme_pos_diff_km_median": float(np.median(dr)),
                  "azel_sep_arcsec_max": float(sep.max()*3600), "azel_sep_arcsec_median": float(np.median(sep)*3600)}))
