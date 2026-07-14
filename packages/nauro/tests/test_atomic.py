"""Tests for the atomic text-write primitive in ``nauro.store._atomic``."""

import threading

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
    """A new modeless file matches plain ``write_text`` permissions exactly,
    so the owner-only tmp creation mode never leaks onto non-sensitive files
    (under the default umask that means bits other than ``0o600``)."""
    control = tmp_path / "control.json"
    control.write_text("{}\n")
    p = tmp_path / "open.json"
    atomic_write_text(p, "{}\n")
    assert (p.stat().st_mode & 0o777) == (control.stat().st_mode & 0o777)


def test_creates_missing_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deeper" / "out.json"
    atomic_write_text(p, "data\n")
    assert p.read_text() == "data\n"


def test_newline_lf_keeps_lf_bytes(tmp_path):
    """``newline="\\n"`` writes LF verbatim so the file is platform-stable."""
    p = tmp_path / "out.html"
    atomic_write_text(p, "a\nb\n", newline="\n")
    assert p.read_bytes() == b"a\nb\n"


def test_target_untouched_when_replace_fails(tmp_path, monkeypatch):
    p = tmp_path / "out.json"

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(_atomic.os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(p, "new\n")
    assert not p.exists()
    assert list(tmp_path.iterdir()) == []  # failed write leaves no tmp sibling


def test_modeless_write_ignores_predictable_tmp_symlink(tmp_path):
    """A symlink pre-planted at the old predictable ``<name>.tmp`` path must not
    redirect a modeless write either. The symlink and its target stay untouched
    and the real file is written via an unguessable temp name."""
    target = tmp_path / "config.json"
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    trap = target.with_suffix(".tmp")
    trap.symlink_to(outside)

    atomic_write_text(target, "payload\n")

    assert outside.read_text() == "original"  # not clobbered through the symlink
    assert target.read_text() == "payload\n"
    assert trap.is_symlink()  # the planted link itself is never touched


def test_mode_none_preserves_existing_permissions(tmp_path):
    p = tmp_path / "out.json"
    p.write_text("old\n")
    p.chmod(0o640)
    atomic_write_text(p, "new\n")
    assert p.read_text() == "new\n"
    assert oct(p.stat().st_mode & 0o777) == "0o640"


def test_explicit_mode_wins_over_existing_permissions(tmp_path):
    p = tmp_path / "out.json"
    p.write_text("old\n")
    p.chmod(0o644)
    atomic_write_text(p, "new\n", mode=0o600)
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_concurrent_writers_land_one_complete_payload(tmp_path):
    """Interleaved writers never corrupt the target: the final content is one
    writer's complete payload and no temp siblings survive."""
    p = tmp_path / "out.json"
    payloads = [f"payload-{i}\n" * 200 for i in range(8)]
    errors: list[BaseException] = []

    def write(payload: str) -> None:
        try:
            for _ in range(20):
                atomic_write_text(p, payload)
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert p.read_text() in payloads
    assert [f.name for f in tmp_path.iterdir()] == ["out.json"]


def test_sensitive_write_ignores_predictable_tmp_symlink(tmp_path):
    """A symlink pre-planted at the old predictable ``<name>.tmp`` path must not
    redirect a mode-restricted write. The symlink target stays untouched and the
    real file is written owner-only via an unguessable temp name."""
    target = tmp_path / "config.json"
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    target.with_suffix(".tmp").symlink_to(outside)

    atomic_write_text(target, "secret\n", mode=0o600)

    assert outside.read_text() == "original"  # not clobbered through the symlink
    assert target.read_text() == "secret\n"
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_sensitive_write_leaves_no_tmp_behind(tmp_path):
    """A successful mode-restricted write leaves only the target — the random
    temp sibling is renamed over it, not left in the directory."""
    target = tmp_path / "config.json"
    atomic_write_text(target, "x\n", mode=0o600)
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]
