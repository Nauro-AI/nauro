"""Tests for the atomic text-write primitive in ``nauro.store._atomic``."""

import pytest

from nauro.store import _atomic
from nauro.store._atomic import atomic_write_text


def test_round_trip_exact_bytes(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_text(p, "abc\n")
    assert p.read_text() == "abc\n"


def test_replace_overwrites_existing_and_removes_tmp(tmp_path):
    p = tmp_path / "out.json"
    p.write_text("old\n")
    atomic_write_text(p, "new\n")
    assert p.read_text() == "new\n"
    assert not p.with_suffix(".tmp").exists()


def test_mode_0o600_applied(tmp_path):
    p = tmp_path / "secret.json"
    atomic_write_text(p, "{}\n", mode=0o600)
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_mode_none_does_not_force_0o600(tmp_path):
    p = tmp_path / "open.json"
    atomic_write_text(p, "{}\n")
    assert p.stat().st_mode & 0o777 != 0o600


def test_creates_missing_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deeper" / "out.json"
    atomic_write_text(p, "data\n")
    assert p.read_text() == "data\n"


def test_target_untouched_when_replace_fails(tmp_path, monkeypatch):
    p = tmp_path / "out.json"

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(_atomic.os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(p, "new\n")
    assert not p.exists()
