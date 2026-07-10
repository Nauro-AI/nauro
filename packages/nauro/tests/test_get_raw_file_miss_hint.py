"""Ordering contract for the ``get_raw_file`` miss-path ``available_files`` hint.

The envelope shape on a miss (``error`` + ``available_files`` siblings) is
pinned in ``test_get_raw_file_parity``; this file pins the *content* contract
of the hint list:

1. Canonical root files lead, in a fixed order, only when present on disk.
2. Other root-level markdown files follow, ascending by name.
3. Each visible subdirectory contributes its lexicographically-last markdown
   path (a concrete path ``get_raw_file`` can fetch; for a flat, zero-padded
   ``decisions/`` layout, the newest decision) plus a ``<dir>/ (N files)``
   roll-up when it holds more than one file, directories ascending by name.
4. ``snapshots/`` and dot-directories are excluded.
5. The 20-entry cap applies after ordering, so root files are never crowded
   out by a large ``decisions/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nauro.mcp.tools import tool_get_raw_file

MISSING_PATH = "does-not-exist.md"

CANONICAL_ROOTS = (
    "project.md",
    "state_current.md",
    "state_history.md",
    "stack.md",
    "open-questions.md",
)


@pytest.fixture
def crowded_store(tmp_path) -> Path:
    """A store with all five canonical roots, a dot-directory, a context/
    directory, a markdown file inside snapshots/, and more decisions than
    the 20-entry cap."""
    store = tmp_path / "store"
    store.mkdir()
    for name in CANONICAL_ROOTS:
        (store / name).write_text(f"# {name}\n")

    backup = store / ".conflict-backup"
    backup.mkdir()
    (backup / "state_current.md").write_text("backup\n")

    context = store / "context"
    context.mkdir()
    (context / "alpha.md").write_text("alpha\n")
    (context / "omega.md").write_text("omega\n")

    snapshots = store / "snapshots"
    snapshots.mkdir()
    (snapshots / "stray.md").write_text("stray\n")

    decisions = store / "decisions"
    decisions.mkdir()
    for num in range(1, 22):
        (decisions / f"{num:03d}-decision-{num}.md").write_text(f"# Decision {num}\n")
    return store


def _available_files(store: Path) -> list[str]:
    envelope = tool_get_raw_file(store, MISSING_PATH)
    assert envelope["error"]["reason"] == f"File not found: {MISSING_PATH}"
    return envelope["available_files"]


def test_full_hint_ordering(crowded_store):
    assert _available_files(crowded_store) == [
        *CANONICAL_ROOTS,
        "context/omega.md",
        "context/ (2 files)",
        "decisions/021-decision-21.md",
        "decisions/ (21 files)",
    ]


def test_canonical_roots_lead_in_fixed_order(crowded_store):
    available = _available_files(crowded_store)
    # Fixed order, not lexicographic (which would put open-questions.md first).
    assert tuple(available[: len(CANONICAL_ROOTS)]) == CANONICAL_ROOTS


def test_missing_canonical_root_is_skipped(crowded_store):
    (crowded_store / "stack.md").unlink()
    available = _available_files(crowded_store)
    assert "stack.md" not in available
    assert available[:4] == [
        "project.md",
        "state_current.md",
        "state_history.md",
        "open-questions.md",
    ]


def test_other_root_files_follow_roots_ascending(crowded_store):
    (crowded_store / "brief.md").write_text("brief\n")
    (crowded_store / "archive.md").write_text("archive\n")
    available = _available_files(crowded_store)
    n = len(CANONICAL_ROOTS)
    assert available[n : n + 2] == ["archive.md", "brief.md"]


def test_dot_directories_and_snapshots_excluded(crowded_store):
    available = _available_files(crowded_store)
    assert not any(entry.startswith(".") for entry in available)
    assert not any("conflict-backup" in entry for entry in available)
    assert not any(entry.startswith("snapshots/") for entry in available)


def test_single_file_subdirectory_gets_no_rollup(crowded_store):
    notes = crowded_store / "notes"
    notes.mkdir()
    (notes / "only.md").write_text("only\n")
    available = _available_files(crowded_store)
    assert "notes/only.md" in available
    assert not any(entry.startswith("notes/ (") for entry in available)


def test_cap_honored_with_many_decisions(crowded_store):
    # 21 decisions collapse to anchor + roll-up, so the whole listing fits.
    available = _available_files(crowded_store)
    assert len(available) <= 20
    assert "decisions/021-decision-21.md" in available
    assert "decisions/ (21 files)" in available


def test_cap_applies_after_ordering_so_roots_survive(crowded_store):
    for num in range(1, 19):
        (crowded_store / f"note-{num:02d}.md").write_text("note\n")
    available = _available_files(crowded_store)
    assert len(available) == 20
    # Canonical roots still lead; the cap truncates from the tail (here the
    # trailing note files and all subdirectory entries).
    assert tuple(available[: len(CANONICAL_ROOTS)]) == CANONICAL_ROOTS


def test_every_path_entry_is_fetchable(crowded_store):
    for entry in _available_files(crowded_store):
        if entry.endswith(" files)"):
            continue
        envelope = tool_get_raw_file(crowded_store, entry)
        assert "error" not in envelope, f"hint entry {entry!r} is not fetchable"
        assert envelope["content"]
