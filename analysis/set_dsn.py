"""Store (or remove) the catalogue database login in Windows Credential Manager.

    python -m analysis.set_dsn           prompts for the connection string (input is hidden)
    python -m analysis.set_dsn --clear   removes it
    python -m analysis.set_dsn --from-file F   reads it from file F (the installer), overwrites F
                                         with zeros and deletes it, then stores it

Prints only "stored" / "cleared" (or a short failure line): never the value.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .db import KEYRING_SERVICE, KEYRING_USER

SCHEMES = ("postgresql://", "postgresql+psycopg2://", "postgres://")


def valid_dsn(dsn: str) -> bool:
    return dsn.startswith(SCHEMES) and not any(c in dsn for c in "\r\n\0")


def take_file(path: Path) -> str:
    """The text of `path`, which is then overwritten with zeros and deleted (even on failure)."""
    try:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("mbcs")  # Inno's SaveStringToFile writes ANSI
        return text.strip()
    finally:
        try:
            with open(path, "r+b") as f:
                n = f.seek(0, os.SEEK_END)
                f.seek(0)
                f.write(b"\0" * n)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass


def store_dsn(dsn: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, dsn.strip())


def clear_dsn() -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except PasswordDeleteError:
        pass  # nothing stored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analysis.set_dsn", description=__doc__.split("\n")[0])
    parser.add_argument("--clear", action="store_true", help="remove the stored connection string")
    parser.add_argument("--from-file", metavar="FILE", help="read it from FILE, then wipe FILE")
    args = parser.parse_args(argv)
    try:
        if args.from_file:
            dsn = take_file(Path(args.from_file))
            if not valid_dsn(dsn):
                print("failed (not a PostgreSQL connection string)")
                return 1
            store_dsn(dsn)
            print("stored")
            return 0
        if args.clear:
            clear_dsn()
            print("cleared")
            return 0
        dsn = getpass.getpass("PostgreSQL connection string (hidden): ").strip()
        if not dsn:
            print("nothing entered; nothing stored")
            return 1
        store_dsn(dsn)
        print("stored")
        return 0
    except Exception as exc:  # the text of a keyring error never carries the value
        print(f"failed ({type(exc).__name__})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
