"""Allocation guarantees for local decision writes.

``_write_decision_direct`` computes the next decision number as
``max(existing num) + 1`` and then writes ``decisions/NNN-slug.md``. The
per-target-file lock in ``write_file`` only excludes writers aiming at the
same filename, so two concurrent local writers with distinct titles compute
the same number and both land — yielding two decisions sharing a number.
``decision_write_lock`` closes that race by serializing the whole
allocate-then-write sequence. These tests pin its contract: concurrent writes
land on distinct numbers, and the lock-held assertion is deterministic — no
sleep-racing.
"""

from __future__ import annotations

import threading

import filelock
import pytest
from nauro_core.constants import DECISIONS_DIR
from nauro_core.operations.propose_decision import _write_decision_direct

from nauro.store.decision_lock import decision_write_lock
from nauro.store.filesystem_store import FilesystemStore


def _make_store(tmp_path, monkeypatch):
    """Point NAURO_HOME and HOME into tmp_path and return a scaffolded store dir."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store_path = tmp_path / "nauro_home" / "projects" / "proj"
    (store_path / DECISIONS_DIR).mkdir(parents=True, exist_ok=True)
    (store_path / "project.md").write_text("# Project\n")
    return store_path


def _lock_path(store_path):
    return store_path / DECISIONS_DIR / ".lock"


def _decision_numbers(store_path):
    """Parse the NNN prefix off each decision file on disk."""
    nums = set()
    for md in (store_path / DECISIONS_DIR).glob("*.md"):
        prefix = md.name.split("-", 1)[0]
        nums.add(int(prefix))
    return nums


def _write_locked(store_path, title):
    with decision_write_lock(store_path):
        _write_decision_direct(
            FilesystemStore(store_path),
            {"title": title, "rationale": f"Rationale for {title}.", "confidence": "medium"},
        )


def test_concurrent_writes_get_distinct_numbers(tmp_path, monkeypatch):
    """Two concurrent writes against one store land on two distinct numbers.

    A barrier maximizes overlap. Without the lock both compute the same next
    number and write distinct slugs that both land; with it they serialize to
    {1, 2}.
    """
    store_path = _make_store(tmp_path, monkeypatch)

    barrier = threading.Barrier(2)

    def _write(title):
        barrier.wait()
        _write_locked(store_path, title)

    threads = [
        threading.Thread(target=_write, args=("First decision",)),
        threading.Thread(target=_write, args=("Second decision",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    decision_files = sorted((store_path / DECISIONS_DIR).glob("*.md"))
    assert len(decision_files) == 2
    assert _decision_numbers(store_path) == {1, 2}


def test_lock_is_held_during_body_and_released_after(tmp_path, monkeypatch):
    """While ``decision_write_lock`` is held a fresh FileLock times out; after exit it acquires."""
    store_path = _make_store(tmp_path, monkeypatch)

    with decision_write_lock(store_path):
        contender = filelock.FileLock(str(_lock_path(store_path)))
        with pytest.raises(filelock.Timeout):
            contender.acquire(timeout=0.2)

    # After exit, a fresh instance acquires immediately.
    after = filelock.FileLock(str(_lock_path(store_path)))
    after.acquire(timeout=0.2)
    assert after.is_locked
    after.release()


def test_serial_writes_are_monotonic(tmp_path, monkeypatch):
    """Three sequential writes produce numbers 1, 2, 3 and three files."""
    store_path = _make_store(tmp_path, monkeypatch)

    for title in ("One", "Two", "Three"):
        _write_locked(store_path, title)

    decision_files = sorted((store_path / DECISIONS_DIR).glob("*.md"))
    assert len(decision_files) == 3
    assert _decision_numbers(store_path) == {1, 2, 3}
