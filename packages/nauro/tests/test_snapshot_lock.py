"""Serialization guarantees for snapshot capture.

``capture_snapshot`` reads the existing snapshots to compute the next dense
version, writes ``snapshots/vNNN.json``, then prunes. Without a lock across
that read-compute-write sequence, two concurrent captures derive the same
version and one silently overwrites the other — losing snapshot history.
These tests pin the contract of ``_snapshot_lock``: it holds an exclusive
file lock spanning compute and write, so concurrent captures land on
distinct versions. The lock-held assertion is deterministic — no sleep-racing.
"""

from __future__ import annotations

import threading

import filelock
import pytest

from nauro.constants import SNAPSHOTS_DIR
from nauro.store.snapshot import (
    _snapshot_lock,
    capture_snapshot,
    list_snapshots,
)


def _make_store(tmp_path, monkeypatch):
    """Point NAURO_HOME and HOME into tmp_path and return a scaffolded store dir."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store_path = tmp_path / "nauro_home" / "projects" / "proj"
    store_path.mkdir(parents=True, exist_ok=True)
    (store_path / "project.md").write_text("# Project\n")
    return store_path


def _lock_path(store_path):
    return store_path / SNAPSHOTS_DIR / ".lock"


def test_concurrent_captures_do_not_overwrite(tmp_path, monkeypatch):
    """Two concurrent captures against one store land on two distinct versions.

    A barrier maximizes overlap. Without the lock both compute the same next
    version and write the same file; with it they serialize to {1, 2}.
    """
    store_path = _make_store(tmp_path, monkeypatch)

    barrier = threading.Barrier(2)

    def _capture():
        barrier.wait()
        capture_snapshot(store_path, trigger="concurrent")

    threads = [threading.Thread(target=_capture) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snapshot_files = sorted((store_path / SNAPSHOTS_DIR).glob("v*.json"))
    assert len(snapshot_files) == 2
    versions = {meta["version"] for meta in list_snapshots(store_path)}
    assert versions == {1, 2}


def test_lock_is_held_during_body_and_released_after(tmp_path, monkeypatch):
    """While ``_snapshot_lock`` is held a fresh FileLock times out; after exit it acquires."""
    store_path = _make_store(tmp_path, monkeypatch)
    snapshots_dir = store_path / SNAPSHOTS_DIR

    with _snapshot_lock(snapshots_dir):
        contender = filelock.FileLock(str(_lock_path(store_path)))
        with pytest.raises(filelock.Timeout):
            contender.acquire(timeout=0.2)

    # After exit, a fresh instance acquires immediately.
    after = filelock.FileLock(str(_lock_path(store_path)))
    after.acquire(timeout=0.2)
    assert after.is_locked
    after.release()


def test_serial_captures_are_monotonic(tmp_path, monkeypatch):
    """Three sequential captures produce versions 1, 2, 3 and three files."""
    store_path = _make_store(tmp_path, monkeypatch)

    versions = [capture_snapshot(store_path, trigger="serial") for _ in range(3)]
    assert versions == [1, 2, 3]

    snapshot_files = sorted((store_path / SNAPSHOTS_DIR).glob("v*.json"))
    assert len(snapshot_files) == 3
