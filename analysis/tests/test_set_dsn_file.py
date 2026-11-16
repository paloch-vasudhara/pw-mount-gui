"""`python -m analysis.set_dsn --from-file` (the installer's route): the file is wiped and deleted,
bad values are refused, no value is echoed. In-memory keyring only."""

from __future__ import annotations

import keyring
import pytest

from analysis import db, set_dsn
from analysis.tests.test_db_dsn import FAKE, MemoryKeyring


@pytest.fixture
def kr():
    mem = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(mem)
    yield mem
    keyring.set_keyring(old)


def test_from_file_stores_wipes_and_deletes(kr, tmp_path, capsys, monkeypatch):
    f = tmp_path / "dsn.tmp"
    f.write_bytes(b"\xef\xbb\xbf" + FAKE.encode() + b"\r\n")
    wiped: list[bytes] = []
    real = set_dsn.take_file

    def spy(p):
        text = real(p)
        wiped.append(p.exists() and p.read_bytes() or b"")
        return text

    monkeypatch.setattr(set_dsn, "take_file", spy)
    assert set_dsn.main(["--from-file", str(f)]) == 0
    assert not f.exists()
    assert wiped == [b""]
    assert kr.get_password(db.KEYRING_SERVICE, db.KEYRING_USER) == FAKE
    out = capsys.readouterr()
    assert out.out.strip() == "stored"
    assert FAKE not in out.out + out.err


def test_take_file_overwrites_with_zeros_before_deleting(tmp_path, monkeypatch):
    f = tmp_path / "x.tmp"
    f.write_bytes(b"postgresql://a:b@h/d")
    seen = {}
    real_unlink = type(f).unlink

    def unlink(self, *a, **k):
        seen["content"] = self.read_bytes()
        real_unlink(self, *a, **k)

    monkeypatch.setattr(type(f), "unlink", unlink)
    assert set_dsn.take_file(f) == "postgresql://a:b@h/d"
    assert seen["content"] == b"\0" * 20
    assert not f.exists()


@pytest.mark.parametrize("bad", ["", "   ", "mysql://u:p@h/d", "postgresql://a\nb", "just text"])
def test_bad_values_are_refused_but_the_file_is_still_wiped(kr, tmp_path, capsys, bad):
    f = tmp_path / "dsn.tmp"
    f.write_text(bad, encoding="utf-8")
    assert set_dsn.main(["--from-file", str(f)]) == 1
    assert not f.exists()
    assert kr.store == {}
    out = capsys.readouterr()
    assert out.out.startswith("failed")
    if bad.strip():
        assert bad.strip() not in out.out + out.err


def test_missing_file_fails_cleanly(kr, tmp_path, capsys):
    assert set_dsn.main(["--from-file", str(tmp_path / "nope.tmp")]) == 1
    assert capsys.readouterr().out.startswith("failed")


@pytest.mark.parametrize("scheme", ["postgresql://", "postgresql+psycopg2://", "postgres://"])
def test_accepted_schemes(kr, tmp_path, scheme):
    f = tmp_path / "d.tmp"
    f.write_text(scheme + "u:p@127.0.0.1:1/x", encoding="utf-8")
    assert set_dsn.main(["--from-file", str(f)]) == 0
