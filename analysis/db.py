"""Read-only database access for the satellite catalogue.

The connection string is a secret (`POSTGRESQL_DSN`, repo-root `.env`). It is loaded here,
only when an engine is created, and is never logged, returned or printed.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
DSN_VAR = "POSTGRESQL_DSN"


class CatalogUnavailable(RuntimeError):
    """The catalogue database is not configured or cannot be reached (maps to HTTP 503)."""


def make_engine() -> Engine:
    load_dotenv(REPO_ROOT / ".env", override=False)
    dsn = os.environ.get(DSN_VAR)
    if not dsn:
        raise CatalogUnavailable("satellite database is not configured")
    # Never include `dsn` (or exceptions that may embed it) in messages.
    engine = create_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        connect_args={"options": "-c default_transaction_read_only=on"}
        if dsn.startswith("postgresql")
        else {},
    )
    return engine
