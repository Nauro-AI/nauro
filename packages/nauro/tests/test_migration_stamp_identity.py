from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from nauro.store import generation_migration_assessment as assessment
from nauro.store.generation_migration_assessment import LegacyMigrationAssessmentError

FIELDS = ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
CONTENT = b"# Project, rewritten\n"
# Windows CPython 3.12+ reports creation time as a path's st_ctime and change time for a handle.
CTIME_HAZARD = os.name == "nt" and sys.version_info >= (3, 12)
REWRITE_ATTEMPTS = 200


def _handle_ctime_differs(path: Path) -> bool:
    descriptor = os.open(path, assessment._READ_FLAGS)
    try:
        return os.fstat(descriptor).st_ctime_ns != path.lstat().st_ctime_ns
    finally:
        os.close(descriptor)


def _rewrite_until(path: Path, content: bytes, moved: Callable[[], bool]) -> None:
    for _ in range(REWRITE_ATTEMPTS):
        time.sleep(0.01)
        path.write_bytes(content)
        if moved():
            return
    pytest.fail(f"change time did not move after {REWRITE_ATTEMPTS} rewrites of {path.name}")


@pytest.fixture
def legacy(tmp_path: Path) -> tuple[Path, Path]:
    store = tmp_path / "store"
    store.mkdir()
    path = store / "project.md"
    path.write_bytes(b"# Project\n")
    if CTIME_HAZARD:
        _rewrite_until(path, CONTENT, lambda: _handle_ctime_differs(path))
    else:
        path.write_bytes(CONTENT)
    return store, path


def _open_with_altered_stat(monkeypatch: pytest.MonkeyPatch, **changes: int) -> None:
    original = os.fstat
    calls = 0

    def altered(descriptor: int) -> object:
        nonlocal calls
        calls += 1
        observed = original(descriptor)
        if calls > 1:
            return observed
        fields = {name: getattr(observed, name) for name in FIELDS}
        fields.update({name: fields[name] + delta for name, delta in changes.items()})
        return SimpleNamespace(**fields)

    monkeypatch.setattr(os, "fstat", altered)


def test_path_and_handle_signatures_agree_for_one_file(legacy: tuple[Path, Path]) -> None:
    store, path = legacy
    if CTIME_HAZARD:
        assert _handle_ctime_differs(path), "the Windows change-time hazard must be present"
    observed = path.lstat()
    descriptor = os.open(path, assessment._READ_FLAGS)
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    assert assessment._stat_signature(opened) == assessment._stat_signature(observed)
    stamp = assessment._read_stamp(store, path)
    assert (stamp.path, stamp.size, stamp.sha256) == (
        "project.md",
        len(CONTENT),
        hashlib.sha256(CONTENT).hexdigest(),
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows path stat reports creation time as st_ctime")
def test_posix_refuses_rewrite_that_moves_only_change_time(
    legacy: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, path = legacy
    original = os.open

    def rewrite_then_open(target: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(target) == path:
            before = path.lstat()

            def only_ctime_moved() -> bool:
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                after = path.lstat()
                return (after.st_size, after.st_mtime_ns, after.st_ino) == (
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ino,
                ) and after.st_ctime_ns != before.st_ctime_ns

            _rewrite_until(path, CONTENT.upper(), only_ctime_moved)
        return original(target, flags, *args)

    monkeypatch.setattr(os, "open", rewrite_then_open)

    with pytest.raises(LegacyMigrationAssessmentError, match="changed during open: project.md"):
        assessment._read_stamp(store, path)


@pytest.mark.parametrize("compare_change_time", [True, False])
@pytest.mark.parametrize("field", ["st_dev", "st_ino", "st_size", "st_mtime_ns"])
def test_identity_or_content_difference_at_open_is_refused(
    legacy: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    compare_change_time: bool,
    field: str,
) -> None:
    store, path = legacy
    monkeypatch.setattr(assessment, "_COMPARE_CHANGE_TIME", compare_change_time)
    _open_with_altered_stat(monkeypatch, **{field: 1})

    with pytest.raises(LegacyMigrationAssessmentError, match="changed during open: project.md"):
        assessment._read_stamp(store, path)


@pytest.mark.parametrize("compare_change_time", [True, False])
def test_change_time_is_compared_only_where_path_and_handle_agree(
    legacy: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    compare_change_time: bool,
) -> None:
    store, path = legacy
    monkeypatch.setattr(assessment, "_COMPARE_CHANGE_TIME", compare_change_time)
    _open_with_altered_stat(monkeypatch, st_ctime_ns=1)

    if compare_change_time:
        with pytest.raises(LegacyMigrationAssessmentError, match="changed during open"):
            assessment._read_stamp(store, path)
    else:
        assert assessment._read_stamp(store, path).size == path.stat().st_size


@pytest.mark.parametrize("compare_change_time", [True, False])
def test_same_size_same_mtime_file_swapped_in_at_open_is_refused(
    legacy: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    compare_change_time: bool,
) -> None:
    store, path = legacy
    other = store.parent / "other.md"
    other.write_bytes(b"# Tampered rewritten\n")
    observed = path.lstat()
    os.utime(other, ns=(observed.st_atime_ns, observed.st_mtime_ns))
    assert other.stat().st_size == observed.st_size
    monkeypatch.setattr(assessment, "_COMPARE_CHANGE_TIME", compare_change_time)
    original = os.open

    def swapped(target: str | os.PathLike[str], flags: int, *args: int) -> int:
        chosen = other if Path(target) == path else target
        return original(chosen, flags, *args)

    monkeypatch.setattr(os, "open", swapped)

    with pytest.raises(LegacyMigrationAssessmentError, match="changed during open: project.md"):
        assessment._read_stamp(store, path)
    assert other.read_bytes() == b"# Tampered rewritten\n"
