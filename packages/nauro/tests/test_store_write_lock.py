"""Lost-update guarantees for shared-file read-modify-write store writes.

``flag_question`` and ``update_state`` read a shared store file, mutate it in
memory, and write it back. The per-target lock in ``write_file`` only excludes
writers aiming at the same filename, so two concurrent local writers each read
the same pre-image and the second write silently clobbers the first — one entry
is lost. ``store_write_lock`` closes that race by serializing the whole
read-modify-write kernel call.

These tests pin the contract: concurrent appends both persist, concurrent state
updates do not drop one another, the lock-file path is distinct from
``write_file``'s per-target lock (so the adapter lock nesting the kernel's write
cannot self-deadlock), and the refactored decision lock keeps its observable
path byte-identical.
"""

from __future__ import annotations

import threading
from pathlib import Path

import filelock
import pytest
from nauro_core.constants import (
    DECISIONS_DIR,
    OPEN_QUESTIONS_MD,
    STATE_CURRENT_FILENAME,
)
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations import update_state as _update_state_op
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.decision_lock import decision_write_lock
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.registry import register_project
from nauro.store.store_lock import RMW_LOCK_SUFFIX, rmw_lock_path, store_write_lock
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _make_store(tmp_path, monkeypatch) -> Path:
    """Point NAURO_HOME/HOME into tmp_path and return a scaffolded store dir."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = register_project("proj", [tmp_path / "repo"])
    scaffold_project_store("proj", store)
    return store


def _question_entries(store_path: Path) -> list[str]:
    """Return the on-disk question entry lines (``- [Q###] ...``)."""
    content = (store_path / OPEN_QUESTIONS_MD).read_text()
    return [line for line in content.split("\n") if line.startswith("- [Q")]


def _flag_locked(store_path: Path, question: str) -> None:
    with store_write_lock(store_path, OPEN_QUESTIONS_MD):
        _flag_question_op(FilesystemStore(store_path), question, None)


def _update_locked(store_path: Path, delta: str) -> None:
    with store_write_lock(store_path, STATE_CURRENT_FILENAME):
        _update_state_op(FilesystemStore(store_path), delta)


def test_concurrent_appends_both_persist(tmp_path, monkeypatch):
    """Two concurrent locked appends both land; exactly two entries on disk.

    A barrier maximizes overlap. Without the lock both read the same
    open-questions.md pre-image and the second write clobbers the first,
    leaving one entry; with it they serialize and both survive.
    """
    store_path = _make_store(tmp_path, monkeypatch)

    barrier = threading.Barrier(2)

    def _flag(question: str) -> None:
        barrier.wait()
        _flag_locked(store_path, question)

    threads = [
        threading.Thread(target=_flag, args=("First question?",)),
        threading.Thread(target=_flag, args=("Second question?",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = _question_entries(store_path)
    assert len(entries) == 2
    joined = "\n".join(entries)
    assert "First question?" in joined
    assert "Second question?" in joined


def test_concurrent_state_updates_serialize(tmp_path, monkeypatch):
    """Two concurrent locked update_state writers do not lose an update.

    Each delta archives the prior body into state_history.md. Serialized, the
    two updates chain: the second update's history carries the first delta, so
    neither is dropped. Without the lock both read the same pre-image and one
    history append is lost.
    """
    store_path = _make_store(tmp_path, monkeypatch)
    (store_path / STATE_CURRENT_FILENAME).write_text("# Current State\n\n- Seed entry\n")

    barrier = threading.Barrier(2)

    def _update(delta: str) -> None:
        barrier.wait()
        _update_locked(store_path, delta)

    threads = [
        threading.Thread(target=_update, args=("Completed alpha milestone",)),
        threading.Thread(target=_update, args=("Completed beta milestone",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    current = (store_path / STATE_CURRENT_FILENAME).read_text()
    history = (store_path / "state_history.md").read_text()
    # The latest update wins state_current.md; the other survives in history.
    # Serialized, every prior body is archived, so both deltas are present
    # across the two files with none silently dropped.
    combined = current + history
    assert "alpha milestone" in combined
    assert "beta milestone" in combined


def test_lock_path_distinct_from_write_file_lock(tmp_path):
    """The RMW lock for a root file is not ``write_file``'s ``<name>.lock``.

    ``write_file`` locks ``open-questions.md.lock``; the adapter
    read-modify-write lock must differ, or the outer lock nesting the kernel's
    inner write on the same path self-deadlocks (flock is not reentrant across
    file descriptors).
    """
    store_path = tmp_path / "store"
    write_file_lock = store_path / (OPEN_QUESTIONS_MD + ".lock")
    rmw_lock = rmw_lock_path(store_path, OPEN_QUESTIONS_MD)

    assert rmw_lock != write_file_lock
    assert rmw_lock == store_path / (OPEN_QUESTIONS_MD + RMW_LOCK_SUFFIX)


def test_decision_lock_path_byte_identical_after_refactor(tmp_path, monkeypatch):
    """``decision_write_lock`` still locks exactly ``decisions/.lock``.

    The refactor delegates to ``store_write_lock`` but the observable lock
    path must not move — a contender on the literal ``decisions/.lock`` still
    times out while the lock is held.
    """
    store_path = _make_store(tmp_path, monkeypatch)
    expected = store_path / DECISIONS_DIR / ".lock"

    with decision_write_lock(store_path):
        contender = filelock.FileLock(str(expected))
        with pytest.raises(filelock.Timeout):
            contender.acquire(timeout=0.2)

    after = filelock.FileLock(str(expected))
    after.acquire(timeout=0.2)
    assert after.is_locked
    after.release()


def test_note_question_branch_exits_zero_and_persists(tmp_path, monkeypatch):
    """``nauro note`` question branch exits 0 and the entry lands on disk."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = register_project("myproj", [repo])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["note", "Should we shard the store?"])
    assert result.exit_code == 0, result.output

    entries = _question_entries(store)
    assert len(entries) == 1
    assert "Should we shard the store?" in entries[0]
