"""DSN lookup order (env -> .env -> Credential Manager) and `python -m analysis.set_dsn`.

An in-memory keyring replaces the Windows one: the real Credential Manager is never touched.
"""

from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend

from analysis import db, set_dsn

FAKE = "postgresql+psycopg2://user:pw@127.0.0.1:1/none"  # never connected: engines are lazy


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        try:
            del self.store[(service, username)]
        except KeyError:
            raise PasswordDeleteError("not found") from None


@pytest.fixture(autouse=True)
def mem_keyring(monkeypatch, tmp_path):
    kr = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(kr)
    monkeypatch.delenv(db.DSN_VAR, raising=False)
    monkeypatch.setattr(db, "REPO_ROOT", tmp_path)  # no .env there
    yield kr
    keyring.set_keyring(old)


def test_keyring_is_the_last_resort(mem_keyring):
    assert db.load_dsn() is None
    mem_keyring.set_password(db.KEYRING_SERVICE, db.KEYRING_USER, "from-keyring")
    assert db.load_dsn() == "from-keyring"


def test_env_beats_keyring(mem_keyring, monkeypatch):
    mem_keyring.set_password(db.KEYRING_SERVICE, db.KEYRING_USER, "from-keyring")
    monkeypatch.setenv(db.DSN_VAR, "from-env")
    assert db.load_dsn() == "from-env"


def test_dotenv_beats_keyring(mem_keyring, monkeypatch, tmp_path):
    mem_keyring.set_password(db.KEYRING_SERVICE, db.KEYRING_USER, "from-keyring")
    (tmp_path / ".env").write_text(f"{db.DSN_VAR}=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv(db.DSN_VAR, "x")  # so monkeypatch removes what load_dotenv sets
    monkeypatch.delenv(db.DSN_VAR)
    assert db.load_dsn() == "from-dotenv"


def test_make_engine_uses_keyring_and_unset_is_unavailable(mem_keyring):
    with pytest.raises(db.CatalogUnavailable):
        db.make_engine()
    mem_keyring.set_password(db.KEYRING_SERVICE, db.KEYRING_USER, FAKE)
    engine = db.make_engine()
    assert str(engine.url).startswith("postgresql")


def test_broken_keyring_means_not_configured(monkeypatch):
    def boom(*a):
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    assert db.load_dsn() is None


def test_set_dsn_stores_and_clears_without_printing_the_value(mem_keyring, monkeypatch, capsys):
    monkeypatch.setattr(set_dsn.getpass, "getpass", lambda prompt="": f"  {FAKE}  ")
    assert set_dsn.main([]) == 0
    assert mem_keyring.get_password(db.KEYRING_SERVICE, db.KEYRING_USER) == FAKE
    assert db.load_dsn() == FAKE
    assert set_dsn.main(["--clear"]) == 0
    assert mem_keyring.get_password(db.KEYRING_SERVICE, db.KEYRING_USER) is None
    out = capsys.readouterr()
    assert out.out.split() == ["stored", "cleared"]
    assert FAKE not in out.out + out.err
    assert set_dsn.main(["--clear"]) == 0  # nothing stored: still fine


def test_set_dsn_empty_input_stores_nothing(mem_keyring, monkeypatch, capsys):
    monkeypatch.setattr(set_dsn.getpass, "getpass", lambda prompt="": "  ")
    assert set_dsn.main([]) == 1
    assert mem_keyring.store == {}
