"""Read-only database access for the satellite catalogue.

The connection string is a secret. Where it comes from, first hit wins:
  1. the environment variable `POSTGRESQL_DSN`
  2. the repo-root `.env` (development only; not shipped in the package)
  3. Windows Credential Manager (`keyring`, service `pw-mount-control`, user `postgresql_dsn`),
     stored once per machine with `python -m analysis.set_dsn`
It is loaded here, only when an engine is created, and is never logged, returned or printed.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
DSN_VAR = "POSTGRESQL_DSN"
KEYRING_SERVICE = "pw-mount-control"
KEYRING_USER = "postgresql_dsn"


class CatalogUnavailable(RuntimeError):
    """The catalogue database is not configured or cannot be reached (maps to HTTP 503)."""


def _keyring_dsn() -> str | None:
    """The stored DSN, or None (no entry, no keyring backend, any keyring failure)."""
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or None
    except Exception:  # never echo details: an error text may touch the secret
        return None


def load_dsn() -> str | None:
    """env -> .env -> Credential Manager. The value is a secret: callers must not log it."""
    load_dotenv(REPO_ROOT / ".env", override=False)
    return os.environ.get(DSN_VAR) or _keyring_dsn()


def make_engine() -> Engine:
    dsn = load_dsn()
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
