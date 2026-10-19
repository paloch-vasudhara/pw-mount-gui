# O1 benchmark — python-sgp4 vs orekit (2026-10-19)

Results are recorded in `docs/BUILD_PLAN.md` §8 "Benchmarks". Scripts kept for re-runs.

```
# out dir outside the repo
python -I tools/bench/gen_and_sgp4.py <out_dir> 20000      # synthetic TLEs + sgp4/numpy az/el timing
<orekit310 env python> -I tools/bench/orekit_bench.py <out_dir> 500 <ssa-toolkit>/orekit-data
python -I tools/bench/compare.py <out_dir>                  # position / az-el agreement
```

`orekit_bench.py` needs the `orekit310` conda env and ssa-toolkit's `orekit-data` (read-only). The app itself
must never import orekit (invariant I9) — this script is a one-off comparison only.
