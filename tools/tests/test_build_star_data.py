import importlib.util
import math
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "build_star_data", Path(__file__).resolve().parents[1] / "build_star_data.py"
)
bsd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsd)

# ruff: noqa: E501
L1 = "H|           1| |00 00 00.22|+01 05 20.4| 9.10| |H|000.00091185|+01.08901332| |   3.54|   -5.20|   -1.88|  1.32|"
L3 = "H|           3| |00 00 01.20|+38 51 33.4| 6.61| |G|000.00500795|+38.85928608| |   2.81|    5.24|   -2.91|  0.53|"
BRIGHT = "H|       32349| |06 45 08.92|-16 42 58.0|-1.44| |H|101.28715533|-16.71611586| | 379.21|-546.01|-1223.07|1.0|"
NO_V = "H|           9| |00 00 01.20|+38 51 33.4|     | |G|000.00500795|+38.85928608| |   2.81|    5.24|   -2.91|  0.53|"
NO_POS = "H|          10| |00 00 01.20|+38 51 33.4| 5.00| |G|            |            | |   2.81|    5.24|   -2.91|  0.53|"


def test_parse_applies_proper_motion_to_j2000():
    ra, dec, v = bsd.parse_line(BRIGHT)
    assert v == -1.44
    dt = 2000.0 - 1991.25
    exp_dec = -16.71611586 + (-1223.07 * dt) / 3.6e6
    exp_ra = 101.28715533 + (-546.01 / math.cos(math.radians(-16.71611586)) * dt) / 3.6e6
    assert abs(dec - exp_dec) < 1e-9 and abs(ra - exp_ra) < 1e-9


def test_skips_missing_fields():
    assert bsd.parse_line(NO_V) is None
    assert bsd.parse_line(NO_POS) is None
    assert bsd.parse_line("garbage") is None


def test_build_cutoff_and_sort():
    out = bsd.build([L1, L3, BRIGHT, NO_V], 6.5)
    assert [s[2] for s in out] == [-1.44]
    out = bsd.build([L1, L3, BRIGHT, NO_V], 9.5)
    assert [s[2] for s in out] == [-1.44, 6.61, 9.1]
