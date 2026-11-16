"""launcher.cleanup: only this app's Credential Manager entries and data folder go.
In-memory keyring and a tmp LOCALAPPDATA; the real vault and profile are never touched."""

from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from launcher import cleanup, paths


class Mem(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self.store[(service, username)]
        except KeyError:
            raise PasswordDeleteError("not found") from None


@pytest.fixture
def env(monkeypatch, tmp_path):
    mem = Mem()
    old = keyring.get_keyring()
    keyring.set_keyring(mem)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    yield mem, tmp_path
    keyring.set_keyring(old)


def test_names_are_the_four_app_logins():
    assert set(cleanup.KEYRING_USERS) == {
        "postgresql_dsn", "astrometry.net", "cctv_password", "adsb_read_token"
    }  # fmt: skip


def test_removes_only_our_entries_and_the_data_folder(env, capsys):
    mem, tmp = env
    for u in cleanup.KEYRING_USERS:
        mem.set_password("pw-mount-control", u, "SECRET-" + u)
    mem.set_password("pw-mount-control", "someone_else", "keep")
    mem.set_password("other-app", "postgresql_dsn", "keep")
    data = paths.data_dir()
    (data / "logs").mkdir(parents=True)
    (data / "settings.json").write_text("{}")
    (tmp / "unrelated").mkdir()
    assert cleanup.main(["--all"]) == 0
    assert set(mem.store) == {("pw-mount-control", "someone_else"), ("other-app", "postgresql_dsn")}
    assert not data.exists()
    assert (tmp / "unrelated").is_dir()
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err
    assert "removed 4" in out.out


def test_nothing_to_remove_is_fine(env, capsys):
    assert cleanup.main(["--all"]) == 0
    assert "removed 0" in capsys.readouterr().out


def test_broken_keyring_does_not_stop_the_folder_removal(env, monkeypatch):
    _, _ = env
    paths.data_dir().mkdir(parents=True)

    def boom(*a):
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    assert cleanup.main(["--all"]) == 0
    assert not paths.data_dir().exists()
