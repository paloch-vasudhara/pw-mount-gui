"""Satellite catalogue: read-only SELECTs on the `satellites` table (SQLAlchemy Core).

Schema mirrors ssa-toolkit `persistence/database.py` `SatelliteRow` @84f2ded (columns used here
only). `object_type` / `orbit_class` are Postgres enums stored by member NAME (which equals the
value: PAYLOAD, LEO, SSO, ...). No DDL, no writes, ever.

Live set = active payloads (`object_type = PAYLOAD AND is_active AND decay_epoch IS NULL`),
mapped to layers: LEO = LEO + SSO, MEO = MEO, GEO = GEO + HEO. UNKNOWN orbit class is excluded.

The "catalogue interface" is `CatalogSource` (fetch_live / search / get); `SqlCatalogSource`
implements it on any SQLAlchemy engine. Tests use a temporary SQLite database with the same
columns. Only counts are ever logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import numpy as np
from sgp4.api import Satrec, SatrecArray
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    case,
    func,
    or_,
    select,
)

from .db import make_engine

log = logging.getLogger("analysis.catalog")

LAYERS: dict[str, tuple[str, ...]] = {
    "leo": ("LEO", "SSO"),
    "meo": ("MEO",),
    "geo": ("GEO", "HEO"),
}
LAYER_OF_CLASS = {cls: layer for layer, classes in LAYERS.items() for cls in classes}
MAX_SEARCH = 500
# search `orbit_group` (lowercase) -> orbit_class values (uppercase); same grouping as the layers
ORBIT_GROUPS = {"leo": ("LEO", "SSO"), "meo": ("MEO",), "geo": ("GEO", "HEO")}
# Live viewer only: objects whose TLE epoch is older than this are not propagated/drawn (old TLEs
# on low orbits propagate to nonsense, O-FAST). Evaluated at every catalogue (re)load. Search, GET
# /api/sats/{norad} and /api/sky/state are NOT filtered.
STALE_TLE_DAYS = 30

# Column subset only; declared as plain strings so the same Core code runs on Postgres and SQLite.
_md = MetaData()
satellites = Table(
    "satellites",
    _md,
    Column("norad_id", Integer, primary_key=True),
    Column("cospar_id", String),
    Column("name", String),
    Column("object_type", String),
    Column("country_code", String),
    Column("tle_line1", String),
    Column("tle_line2", String),
    Column("tle_epoch", DateTime(timezone=True)),
    Column("orbit_class", String),
    Column("is_active", Boolean),
    Column("decay_epoch", DateTime(timezone=True)),
)
_t = satellites.c
_LIVE = and_(_t.is_active.is_(True), _t.decay_epoch.is_(None))  # never decayed (G6/I8)

OBJECT_COLS = (
    _t.norad_id,
    _t.name,
    _t.cospar_id,
    _t.country_code,
    _t.object_type,
    _t.orbit_class,
    _t.tle_epoch,
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def tle_age_days(epoch: Any, now: datetime | None = None) -> float | None:
    """Age of the TLE epoch in days (None when unknown). Naive datetimes are taken as UTC."""
    if not isinstance(epoch, datetime):
        return None
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)
    return round(((now or datetime.now(UTC)) - epoch).total_seconds() / 86400.0, 3)


def _info(row: Any) -> dict:
    return {
        "norad": int(row.norad_id),
        "name": row.name,
        "cospar": row.cospar_id,
        "country": row.country_code,
        "object_type": row.object_type,
        "orbit_class": row.orbit_class,
        "tle_epoch": _iso(row.tle_epoch),
        "tle_age_days": tle_age_days(row.tle_epoch),
    }


class CatalogSource(Protocol):
    def fetch_live(self) -> list[tuple]: ...

    def search(
        self,
        q: str | None,
        orbit_class: str | None,
        object_type: str | None,
        limit: int,
        country: str | None = None,
        orbit_group: str | None = None,
    ) -> list[dict]: ...

    def get(self, norad: int) -> dict | None: ...

    def countries(self) -> list[dict]: ...


class SqlCatalogSource:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def fetch_live(self) -> list[tuple]:
        """Live payload rows: (norad, name, cospar, country, orbit_class, line1, line2).

        Excludes decayed/inactive objects and TLEs older than STALE_TLE_DAYS (or with no epoch).
        """
        cutoff = datetime.now(UTC) - timedelta(days=STALE_TLE_DAYS)
        stmt = (
            select(
                _t.norad_id,
                _t.name,
                _t.cospar_id,
                _t.country_code,
                _t.orbit_class,
                _t.tle_line1,
                _t.tle_line2,
            )
            .where(
                _t.object_type == "PAYLOAD",
                _LIVE,
                _t.tle_epoch >= cutoff,
                _t.orbit_class.in_(list(LAYER_OF_CLASS)),
            )
            .order_by(_t.norad_id)
        )
        with self.engine.connect() as c:
            return [tuple(r) for r in c.execute(stmt)]

    def search(
        self,
        q: str | None,
        orbit_class: str | None,
        object_type: str | None,
        limit: int,
        country: str | None = None,
        orbit_group: str | None = None,
    ) -> list[dict]:
        conds = [_LIVE]
        # exact matches (name, COSPAR, NORAD id) first: the limit never cuts them
        exact_first = []
        if q and q.strip():
            q = q.strip()
            terms = [_t.name.ilike(f"%{q}%"), _t.cospar_id.ilike(f"%{q}%")]
            exact = [func.upper(_t.name) == q.upper(), func.upper(_t.cospar_id) == q.upper()]
            if q.isdigit():
                terms.append(_t.norad_id == int(q))
                exact.append(_t.norad_id == int(q))
            conds.append(or_(*terms))
            exact_first.append(case((or_(*exact), 0), else_=1))
        if orbit_class:
            conds.append(_t.orbit_class == orbit_class.upper())
        if object_type:
            conds.append(_t.object_type == object_type.upper())
        if country and country.strip():
            conds.append(func.upper(_t.country_code) == country.strip().upper())
        if orbit_group:
            conds.append(_t.orbit_class.in_(ORBIT_GROUPS[orbit_group.lower()]))
        stmt = (
            select(*OBJECT_COLS)
            .where(*conds)
            .order_by(*exact_first, _t.norad_id.desc())
            .limit(max(1, min(limit, MAX_SEARCH)))
        )
        with self.engine.connect() as c:
            return [_info(r) for r in c.execute(stmt)]

    def get(self, norad: int) -> dict | None:
        stmt = select(*OBJECT_COLS, _t.tle_line1, _t.tle_line2).where(_t.norad_id == norad, _LIVE)
        with self.engine.connect() as c:
            r = c.execute(stmt).first()
        if r is None:
            return None
        out = _info(r)
        out["tle_line1"], out["tle_line2"] = r.tle_line1, r.tle_line2
        return out

    def countries(self) -> list[dict]:
        """Distinct non-null country codes of live objects: [{code, count}], most common first."""
        n = func.count()
        stmt = (
            select(_t.country_code, n.label("n"))
            .where(_LIVE, _t.country_code.is_not(None), _t.country_code != "")
            .group_by(_t.country_code)
            .order_by(n.desc(), _t.country_code)
        )
        with self.engine.connect() as c:
            return [{"code": r[0], "count": int(r[1])} for r in c.execute(stmt)]

    def count_live(self) -> int:
        stmt = select(func.count()).select_from(satellites).where(_LIVE)
        with self.engine.connect() as c:
            return int(c.execute(stmt).scalar())


def default_source() -> CatalogSource:
    """Picklable factory: the real database via POSTGRESQL_DSN (loaded inside make_engine)."""
    return SqlCatalogSource(make_engine())


@dataclass
class LayerData:
    layer: str
    norad: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    names: list[str] = field(default_factory=list)
    cospar: list[str | None] = field(default_factory=list)
    country: list[str | None] = field(default_factory=list)
    orbit_class: list[str] = field(default_factory=list)
    blocks: list[SatrecArray] = field(default_factory=list)  # empty in the API process

    def __len__(self) -> int:
        return len(self.norad)

    def objects(self) -> list[list]:
        return [
            [int(n), nm, cs, co, oc]
            for n, nm, cs, co, oc in zip(
                self.norad, self.names, self.cospar, self.country, self.orbit_class, strict=True
            )
        ]


BLOCK = 2048


def build_layers(rows: list[tuple], with_satrecs: bool) -> dict[str, LayerData]:
    """Group live rows into layers; optionally build the sgp4 SatrecArray blocks."""
    per: dict[str, list[tuple]] = {layer: [] for layer in LAYERS}
    bad = 0
    for row in rows:
        layer = LAYER_OF_CLASS.get(row[4])
        if layer is None:
            continue
        sat = None
        if with_satrecs:
            try:
                sat = Satrec.twoline2rv(row[5], row[6])
            except Exception:
                bad += 1
                continue
        per[layer].append((*row[:5], sat))
    out: dict[str, LayerData] = {}
    for layer, items in per.items():
        d = LayerData(layer)
        d.norad = np.array([i[0] for i in items], dtype=np.int64)
        d.names = [i[1] for i in items]
        d.cospar = [i[2] for i in items]
        d.country = [i[3] for i in items]
        d.orbit_class = [i[4] for i in items]
        if with_satrecs:
            sats = [i[5] for i in items]
            d.blocks = [SatrecArray(sats[k : k + BLOCK]) for k in range(0, len(sats), BLOCK)]
        out[layer] = d
    if bad:
        log.warning("skipped %d unparseable TLEs", bad)
    return out


def load_layers(source: CatalogSource, with_satrecs: bool) -> dict[str, LayerData]:
    layers = build_layers(source.fetch_live(), with_satrecs)
    log.info("catalogue loaded: %s", {k: len(v) for k, v in layers.items()})
    return layers
