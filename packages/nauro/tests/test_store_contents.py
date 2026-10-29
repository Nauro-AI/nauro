"""Tests for the doctor's recognizer of Nauro-managed store files."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from nauro.cli._store_contents import is_managed_store_path, unknown_store_files
from nauro.templates.scaffolds import scaffold_project_store

MANAGED = [
    "project.md",
    "state.md",
    "state_current.md",
    "state_history.md",
    "stack.md",
    "open-questions.md",
    "questions-provenance.json",
    "decisions/001-initial-setup.md",
    "context/auth-brief.md",
    ".decision-hashes.json",
    ".sync-state.json",
    ".sync-state.json.lock",
    "snapshots/v001.json",
    "snapshots/.lock",
    "journal/events.jsonl",
    "journal/.lock",
    ".conflict-backup/decisions/002-x.md",
    ".pull-spool-abc123/002-x.md",
    "nauro-graph.html",
    "project.md.lock",
    ".decision-hashes.json.lock",
    "state_current.md.rmwlock",
    "decisions/.lock",
    ".project.md.0123456789abcdef.tmp",
    "decisions/.001-initial-setup.md.0123456789abcdef.tmp",
    ".replica-control.lock",
    ".replica/authority.json",
]

UNKNOWN = [
    "adoption-review.md",
    "decisions/sub/x.md",
    "foo.lock",
    ".hidden",
    ".project.md.zz.tmp",
    "context/Not A Slug.md",
    "handoffs/old.md",
    "__pycache__/x",
    "snapshots",
    "notes/nauro-graph.html",
]


@pytest.mark.parametrize("rel", MANAGED)
def test_managed_path_is_exempt(rel: str) -> None:
    assert is_managed_store_path(rel)


@pytest.mark.parametrize("rel", UNKNOWN)
def test_near_miss_is_flagged(rel: str) -> None:
    assert not is_managed_store_path(rel)


def test_scaffolded_store_has_no_unknown_files(tmp_path: Path) -> None:
    scaffold_project_store("p", tmp_path)
    assert unknown_store_files(tmp_path) == ()


def test_walk_lists_strays_sorted_and_skips_managed_trees(tmp_path: Path) -> None:
    scaffold_project_store("p", tmp_path)
    (tmp_path / "snapshots" / "v001.json").write_text("{}")
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal" / "anything.txt").write_text("x")
    (tmp_path / "handoffs").mkdir()
    (tmp_path / "handoffs" / "old.md").write_text("x")
    (tmp_path / "adoption-review.md").write_text("x")
    (tmp_path / "decisions" / "notes.txt").write_text("x")

    assert unknown_store_files(tmp_path) == (
        "adoption-review.md",
        "decisions/notes.txt",
        "handoffs/old.md",
    )


def test_walk_does_not_follow_directory_links(tmp_path: Path) -> None:
    store = tmp_path / "store"
    scaffold_project_store("p", store)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("x")
    os.symlink(outside, store / "linked", target_is_directory=True)

    assert unknown_store_files(store) == ("linked",)


class _ReparseEntry:
    def __init__(self, entry: os.DirEntry[str]) -> None:
        self.name, self.path, self._entry = entry.name, entry.path, entry

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def stat(self, follow_symlinks: bool = True) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)


def test_walk_lists_a_reparse_point_without_entering_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold_project_store("p", tmp_path)
    (tmp_path / "junction").mkdir()
    (tmp_path / "junction" / "outside.md").write_text("x")
    real_scandir = os.scandir

    class _Listing(list):
        def __enter__(self) -> _Listing:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def scandir(path: Path) -> _Listing:
        with real_scandir(path) as entries:
            return _Listing(_ReparseEntry(e) if e.name == "junction" else e for e in entries)

    monkeypatch.setattr(os, "scandir", scandir)

    assert unknown_store_files(tmp_path) == ("junction",)


def test_walk_lists_an_unreadable_directory_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scaffold_project_store("p", tmp_path)
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "inside.md").write_text("x")
    (tmp_path / "adoption-review.md").write_text("x")
    real_scandir = os.scandir

    def scandir(path: Path) -> object:
        if Path(path).name == "locked":
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)

    assert unknown_store_files(tmp_path) == ("adoption-review.md", "locked/")
