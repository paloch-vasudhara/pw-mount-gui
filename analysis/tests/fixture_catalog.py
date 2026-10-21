# ruff: noqa: E501
"""Fixture catalogue for tests: synthetic-but-realistic TLEs in a temporary SQLite database.

The SQLite file is created here (test side only) with the same columns the analysis service
reads. The real database is never touched by automated tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy import create_engine, text

from analysis.catalog import SqlCatalogSource

EPOCH_YEAR2 = 26
EPOCH_DAY = 294.0  # 2026-10-21 00:00 UTC
EPOCH_DT = datetime(2026, 10, 21, tzinfo=UTC)


def _cks(line: str) -> int:
    return sum(int(ch) if ch.isdigit() else (1 if ch == "-" else 0) for ch in line) % 10


def make_tle(
    norad: int,
    incl: float,
    raan: float,
    ecc: float,
    argp: float,
    ma: float,
    mm: float,
    bstar: str = " 12345-4",
) -> tuple[str, str]:
    l1 = (
        f"1 {norad:05d}U 20001A   {EPOCH_YEAR2:02d}{EPOCH_DAY:012.8f}  .00001000"
        f"  00000-0 {bstar} 0  999"
    )
    l2 = (
        f"2 {norad:05d} {incl:8.4f} {raan:8.4f} {int(round(ecc * 1e7)):07d} "
        f"{argp:8.4f} {ma:8.4f} {mm:11.8f}    1"
    )
    l1 = l1[:68].ljust(68)
    l2 = l2[:68].ljust(68)
    return l1 + str(_cks(l1)), l2 + str(_cks(l2))


# (norad, name, orbit_class, object_type, active, decayed, elements...)
# elements: incl, raan, ecc, argp, ma, mean motion (rev/day)
ROWS = [
    (25544, "ISS (ZARYA)", "LEO", "PAYLOAD", True, False, (51.6416, 247.4627, 0.0006703, 130.5360, 325.0288, 15.50)),
    (43013, "SSO-SAT A", "SSO", "PAYLOAD", True, False, (98.2000, 120.0000, 0.0001200, 90.0000, 270.0000, 14.57)),
    (24876, "NAVSTAR 43 (GPS)", "MEO", "PAYLOAD", True, False, (55.0000, 60.0000, 0.0080000, 45.0000, 100.0000, 2.00563)),
    (41000, "GEO-SAT E100", "GEO", "PAYLOAD", True, False, (0.0500, 80.0000, 0.0002000, 0.0000, 49.5000, 1.00271)),
    (41001, "GEO-SAT E120", "GEO", "PAYLOAD", True, False, (0.0300, 70.0000, 0.0003000, 0.0000, 79.5000, 1.00272)),
    (39000, "MOLNIYA 1-91", "HEO", "PAYLOAD", True, False, (63.4000, 100.0000, 0.7200000, 270.0000, 10.0000, 2.00600)),
    # live-looking but with a stale TLE epoch (> 30 d): absent from chunks/objects, still searchable
    (44444, "STALE TLE SAT", "LEO", "PAYLOAD", True, False, (51.6000, 100.0000, 0.0010000, 100.0000, 50.0000, 15.50)),
    # never drawn / returned:
    (11111, "DECAYED SAT", "LEO", "PAYLOAD", False, True, (51.6000, 100.0000, 0.0010000, 100.0000, 100.0000, 15.60)),
    (22222, "SOME ROCKET BODY", "LEO", "ROCKET_BODY", True, False, (51.6000, 250.0000, 0.0010000, 100.0000, 320.0000, 15.40)),
    (33333, "UNKNOWN CLASS SAT", "UNKNOWN", "PAYLOAD", True, False, (51.6000, 250.0000, 0.0010000, 100.0000, 320.0000, 15.40)),
]


STALE_NORADS = {44444}


def tle_of(norad: int) -> tuple[str, str]:
    for r in ROWS:
        if r[0] == norad:
            return make_tle(r[0], *r[6])
    raise KeyError(norad)


def build_sqlite(path: str, extra_leo: int = 0) -> None:
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE TABLE satellites (norad_id INTEGER PRIMARY KEY, cospar_id TEXT, name TEXT,"
                " object_type TEXT, country_code TEXT, tle_line1 TEXT, tle_line2 TEXT,"
                " tle_epoch TIMESTAMP, orbit_class TEXT, is_active BOOLEAN, decay_epoch TIMESTAMP)"
            )
        )
        ins = text(
            "INSERT INTO satellites VALUES (:n,:cs,:nm,:ot,:co,:l1,:l2,:ep,:oc,:act,:dec)"
        )
        fresh = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")  # DB epoch column: "now"
        stale = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        for norad, name, oc, ot, active, decayed, el in ROWS:
            l1, l2 = make_tle(norad, *el)
            c.execute(
                ins,
                dict(
                    n=norad, cs=f"2020-001{chr(65 + norad % 20)}", nm=name, ot=ot, co="TST",
                    l1=l1, l2=l2, ep=stale if norad in STALE_NORADS else fresh, oc=oc, act=active,
                    dec="2026-09-01 00:00:00" if decayed else None,
                ),
            )
        # Synthetic bulk LEO (many objects) for load tests.
        for k in range(extra_leo):
            norad = 60000 + k
            raan = (k * 7.3) % 360
            ma = (k * 11.7) % 360
            l1, l2 = make_tle(norad, 51.6 + (k % 40), raan, 0.001, 100.0, ma, 15.2 + (k % 5) * 0.1)
            c.execute(
                ins,
                dict(
                    n=norad, cs="2020-999Z", nm=f"BULK {k}", ot="PAYLOAD", co="TST", l1=l1, l2=l2,
                    ep=fresh, oc="LEO", act=True, dec=None,
                ),
            )
    eng.dispose()


def sqlite_source(path: str) -> SqlCatalogSource:
    return SqlCatalogSource(create_engine(f"sqlite:///{path}"))


def factory_for(path: str):
    """Picklable (module-level function + str) factory for the process pool child."""
    return partial(sqlite_source, path)
