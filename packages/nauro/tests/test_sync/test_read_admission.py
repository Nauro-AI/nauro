"""Path admission on the read and listing surfaces.

The raw-file read classifies and admits before any resolve, stat or read, the
miss hint enumerates through the admission walker, and the conflict-backup
listing runs over one admitted plain directory. Calls the tool layer directly;
nothing here starts the stdio server.
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from nauro_core.renderers import RENDERERS
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import tools as tools_module
from nauro.mcp.tools import tool_get_raw_file
from nauro.store import filesystem_store
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
)
from nauro.sync import merge as merge_module
from nauro.sync import quarantine as quarantine_module
from nauro.sync._path_diagnostics import _StoreRootPreparationError
from nauro.sync.merge import CONFLICT_BACKUP_DIR, write_backup
from nauro.sync.quarantine import (
    list_conflict_backup_files,
    list_quarantine_backups,
    save_quarantine_backup,
    unresolved_quarantines,
)
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename semantics")
LINK_KINDS = ["symlink", "junction"]
UPPERCASE_SUFFIX_ROW = pytest.param(
    "NOTE.MD",
    marks=pytest.mark.skipif(
        sys.platform != "win32", reason="only Windows matches a filename suffix case-insensitively"
    ),
)

MISSING = "does-not-exist.md"
ROOT_UNAVAILABLE = "The Store root is unavailable."
PROJECT_BODY = "# project\n"
QUESTIONS_BODY = "# open questions\n"
ALPHA_BODY = "alpha\n"

ESCAPING_ROWS = ["../../etc/passwd", "../x", "a/../../x"]

CANONICAL_ROOTS = (
    "project.md",
    "state_current.md",
    "state_history.md",
    "stack.md",
    "open-questions.md",
)

ORDINARY_HINT = [
    *CANONICAL_ROOTS,
    "context/omega.md",
    "context/ (2 files)",
    "decisions/021-decision-21.md",
    "decisions/ (21 files)",
]

RESERVED_ROWS = [
    f"{_REPLICA_CONTROL_ROOT_NAME}/authority.json",
    _REPLICA_CONTROL_LOCK_NAME,
    " .REPLICA. /x",
    ".replica:ads/x",
]

UNSAFE_ROWS = [
    "C:x",
    "C:../x",
    "\\\\server\\share\\x",
    "\\\\server\\share\\..\\x",
    "context/ :stream",
    "../../etc/passwd",
    "foo\x00bar",
]


def _store(tmp_path: Path) -> Path:
    """A small store carrying one real control file."""
    store = tmp_path / "store"
    store.mkdir()
    (store / "project.md").write_text(PROJECT_BODY, encoding="utf-8")
    (store / "open-questions.md").write_text(QUESTIONS_BODY, encoding="utf-8")
    context = store / "context"
    context.mkdir()
    (context / "alpha.md").write_text(ALPHA_BODY, encoding="utf-8")
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    (control / "authority.json").write_text("{}\n", encoding="utf-8")
    return store


def _crowded_store(tmp_path: Path) -> Path:
    """The hint-ordering fixture, plus control and backup content to prune."""
    store = tmp_path / "store"
    store.mkdir()
    for name in CANONICAL_ROOTS:
        (store / name).write_text(f"# {name}\n", encoding="utf-8")
    context = store / "context"
    context.mkdir()
    (context / "alpha.md").write_text("alpha\n", encoding="utf-8")
    (context / "omega.md").write_text("omega\n", encoding="utf-8")
    snapshots = store / "snapshots"
    snapshots.mkdir()
    (snapshots / "stray.md").write_text("stray\n", encoding="utf-8")
    decisions = store / "decisions"
    decisions.mkdir()
    for num in range(1, 22):
        (decisions / f"{num:03d}-decision-{num}.md").write_text(
            f"# Decision {num}\n", encoding="utf-8"
        )
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    (control / "notes.md").write_text("control\n", encoding="utf-8")
    backup = store / CONFLICT_BACKUP_DIR
    backup.mkdir()
    (backup / "state_current.md").write_text("backup\n", encoding="utf-8")
    return store


def _scaffold(name: str = "readproj", *, repo: Path) -> Path:
    _pid, store = register_project_v2(name, [repo])
    scaffold_project_store(name, store)
    return store


def _status_store(tmp_path: Path, monkeypatch) -> Path:
    """A registered, authenticated project whose status output is readable."""
    store = _scaffold(repo=tmp_path)
    save_config(
        {
            "auth": {
                "sub": "auth0|test",
                "access_token": "tok_orig",
                "refresh_token": "refresh_orig",
            }
        }
    )
    monkeypatch.chdir(tmp_path)
    return store


def _link(link: Path, target: Path, kind: str) -> None:
    """Point ``link`` at ``target`` as a symlink or as a Windows junction."""
    if kind == "junction":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
        return
    link.symlink_to(target, target_is_directory=True)


def _require_link_kind(kind: str) -> None:
    if (kind == "junction") != (sys.platform == "win32"):
        pytest.skip(f"{kind} is not this platform's directory link")


def _control_roots(store: Path) -> tuple[Path, Path]:
    return (store / _REPLICA_CONTROL_ROOT_NAME, store / _REPLICA_CONTROL_LOCK_NAME)


def _names_control(value: str) -> bool:
    """True when the last component folds to a reserved control name."""
    base = os.path.basename(value).split(":", 1)[0]
    folded = base.lstrip(" ").rstrip(" .").casefold()
    return folded in {_REPLICA_CONTROL_ROOT_NAME.casefold(), _REPLICA_CONTROL_LOCK_NAME.casefold()}


def _forbid_access(monkeypatch, *roots: Path, exact: bool = False) -> None:
    """Fail the test if anything stats or reads under one of ``roots``."""
    real_lstat = merge_module.os.lstat
    real_read_bytes = Path.read_bytes
    real_read_text = filesystem_store.read_text_lenient
    # Spelled comparison, not realpath: resolving inside the stat guard would
    # call the patched lstat again and recurse.
    spelled = {str(root) for root in roots} | {os.path.realpath(root) for root in roots}

    def check(value) -> None:
        target = os.fspath(value)
        under = any(
            target.startswith(f"{root}{os.sep}") or (exact and target == root) for root in spelled
        )
        # An alias spelling names the same control node without living under
        # its path, so the name decides too.
        if under or (exact and _names_control(target)):
            pytest.fail(f"reached {value!r}")

    def read_check(value) -> None:
        # Reads resolve first: a read through a link would not be spelled
        # under any forbidden root.
        resolved = os.path.realpath(value)
        if any(resolved == root or resolved.startswith(f"{root}{os.sep}") for root in spelled):
            pytest.fail(f"read {value!r}")
        check(value)

    def guarded_lstat(path, *args, **kwargs):
        check(path)
        return real_lstat(path, *args, **kwargs)

    def read_bytes(self):
        read_check(self)
        return real_read_bytes(self)

    def read_text_lenient(path):
        read_check(path)
        return real_read_text(path)

    monkeypatch.setattr(merge_module.os, "lstat", guarded_lstat)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(filesystem_store, "read_text_lenient", read_text_lenient)


class _GuardedEntry:
    """A directory entry whose ``stat`` fails the test for one forbidden name."""

    def __init__(self, entry, forbidden: str) -> None:
        self._entry = entry
        self._forbidden = forbidden

    @property
    def name(self) -> str:
        return self._entry.name

    @property
    def path(self) -> str:
        return self._entry.path

    def stat(self, **kwargs):
        if self._entry.name == self._forbidden:
            pytest.fail(f"stat {self._entry.name!r}")
        return self._entry.stat(**kwargs)


def _forbid_entry_stat(monkeypatch, directory: Path, forbidden: str) -> None:
    real_scandir = os.scandir

    @contextmanager
    def scandir(path):
        with real_scandir(path) as listing:
            entries = list(listing)
        if Path(path) == directory:
            yield [_GuardedEntry(entry, forbidden) for entry in entries]
        else:
            yield entries

    monkeypatch.setattr(quarantine_module.os, "scandir", scandir)


def _assert_unlistable(store: Path) -> None:
    """Every backup surface reports a failure, and neither command claims none."""
    for listing in (list_conflict_backup_files, list_quarantine_backups, unresolved_quarantines):
        with pytest.raises(OSError):
            listing(store)
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert "Quarantined decision-number collisions: could not be read" in status.output
    quiet = runner.invoke(app, ["sync", "--status"])
    assert quiet.exit_code == 0, quiet.output
    assert "Conflict backups" not in quiet.output
    assert "Quarantined decision-number collisions" not in quiet.output


def _without_reason(envelope: dict) -> dict:
    """The envelope with the path-bearing reason dropped, for key-for-key equality."""
    shape = dict(envelope)
    shape["error"] = {key: value for key, value in shape["error"].items() if key != "reason"}
    return shape


class TestRawFileRows:
    @pytest.mark.parametrize("row", RESERVED_ROWS)
    def test_reserved_path_answers_exactly_like_a_miss(self, tmp_path, monkeypatch, row):
        store = _store(tmp_path)
        _forbid_access(monkeypatch, *_control_roots(store), exact=True)
        reserved = tool_get_raw_file(store, row)
        missing = tool_get_raw_file(store, MISSING)
        assert reserved["error"]["reason"] == f"File not found: {row}"
        assert missing["error"]["reason"] == f"File not found: {MISSING}"
        assert _without_reason(reserved) == _without_reason(missing)
        assert "project.md" in reserved["available_files"]

    @pytest.mark.parametrize("row", UNSAFE_ROWS)
    def test_unsafe_path_keeps_the_existing_refusal(self, tmp_path, monkeypatch, row):
        store = _store(tmp_path)
        _forbid_access(monkeypatch, store)
        envelope = tool_get_raw_file(store, row)
        assert envelope["error"] == {"kind": "error", "reason": f"Invalid path: {row}"}
        assert "available_files" not in envelope
        assert "content" not in envelope

    def test_ordinary_file_still_reads(self, tmp_path):
        store = _store(tmp_path)
        envelope = tool_get_raw_file(store, "project.md")
        assert envelope["content"] == PROJECT_BODY
        assert "error" not in envelope

    @pytest.mark.parametrize("row", ["context", "context/"])
    def test_directory_answers_like_a_miss(self, tmp_path, row):
        store = _store(tmp_path)
        envelope = tool_get_raw_file(store, row)
        assert envelope["error"]["reason"] == f"File not found: {row}"
        assert _without_reason(envelope) == _without_reason(tool_get_raw_file(store, MISSING))


class TestCollapseRows:
    def test_parent_component_reads_the_file_it_names(self, tmp_path):
        store = _store(tmp_path)
        envelope = tool_get_raw_file(store, "context/../open-questions.md")
        assert envelope["content"] == QUESTIONS_BODY
        assert "error" not in envelope

    @pytest.mark.parametrize(
        ("row", "body"), [("./project.md", PROJECT_BODY), ("context/./alpha.md", ALPHA_BODY)]
    )
    def test_dot_component_reads_the_file_it_names(self, tmp_path, row, body):
        assert tool_get_raw_file(_store(tmp_path), row)["content"] == body

    @pytest.mark.parametrize("row", ESCAPING_ROWS)
    def test_escaping_spelling_is_refused_untouched(self, tmp_path, monkeypatch, row):
        store = _store(tmp_path)
        _forbid_access(monkeypatch, store)
        envelope = tool_get_raw_file(store, row)
        assert envelope["error"] == {"kind": "error", "reason": f"Invalid path: {row}"}
        assert "available_files" not in envelope

    @POSIX_ONLY
    def test_collapse_never_traverses_a_link_in_the_spelling(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        (store / "alias").symlink_to(store / _REPLICA_CONTROL_ROOT_NAME, target_is_directory=True)
        _forbid_access(monkeypatch, *_control_roots(store), exact=True)
        assert tool_get_raw_file(store, "alias/../project.md")["content"] == PROJECT_BODY
        envelope = tool_get_raw_file(store, "alias/../authority.json")
        assert envelope["error"]["reason"] == "File not found: alias/../authority.json"
        assert _without_reason(envelope) == _without_reason(tool_get_raw_file(store, MISSING))


class TestLinkRows:
    @POSIX_ONLY
    def test_alias_to_control_answers_like_a_miss(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        (store / "alias").symlink_to(store / _REPLICA_CONTROL_ROOT_NAME / "authority.json")
        _forbid_access(monkeypatch, *_control_roots(store), exact=True)
        envelope = tool_get_raw_file(store, "alias")
        assert envelope["error"]["reason"] == "File not found: alias"
        assert _without_reason(envelope) == _without_reason(tool_get_raw_file(store, MISSING))

    @POSIX_ONLY
    def test_alias_out_of_store_is_refused(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret\n", encoding="utf-8")
        (store / "escape").symlink_to(outside / "secret")
        _forbid_access(monkeypatch, outside, exact=True)
        envelope = tool_get_raw_file(store, "escape")
        assert envelope["error"] == {"kind": "error", "reason": "Invalid path: escape"}
        assert "available_files" not in envelope

    @POSIX_ONLY
    def test_link_to_an_in_store_file_still_reads(self, tmp_path):
        store = _store(tmp_path)
        (store / "inlink").symlink_to(store / "project.md")
        assert tool_get_raw_file(store, "inlink")["content"] == PROJECT_BODY

    @pytest.mark.parametrize("kind", LINK_KINDS)
    def test_directory_link_into_control_answers_like_a_miss(self, tmp_path, monkeypatch, kind):
        _require_link_kind(kind)
        store = _store(tmp_path)
        _link(store / "ctrl", store / _REPLICA_CONTROL_ROOT_NAME, kind)
        _forbid_access(monkeypatch, *_control_roots(store), exact=True)
        envelope = tool_get_raw_file(store, "ctrl/authority.json")
        assert envelope["error"]["reason"] == "File not found: ctrl/authority.json"
        assert _without_reason(envelope) == _without_reason(tool_get_raw_file(store, MISSING))


class TestRootRows:
    def test_unavailable_root_is_reported_without_reading(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        _forbid_access(monkeypatch, store)

        def raise_unavailable(configured_root: Path):
            raise _StoreRootPreparationError()

        monkeypatch.setattr(tools_module, "_prepare_store_root", raise_unavailable)
        envelope = tool_get_raw_file(store, "project.md")
        assert envelope["store"] == "local"
        assert envelope["error"] == {"kind": "error", "reason": ROOT_UNAVAILABLE}
        assert "available_files" not in envelope
        assert "content" not in envelope

    def test_store_path_that_is_a_file_reports_and_renders_the_same(self, tmp_path):
        store = tmp_path / "store"
        store.write_text("not a directory", encoding="utf-8")
        envelope = tool_get_raw_file(store, "project.md")
        assert envelope["error"] == {"kind": "error", "reason": ROOT_UNAVAILABLE}
        assert "available_files" not in envelope
        assert RENDERERS["get_raw_file"](envelope) == f"Error: {ROOT_UNAVAILABLE}"


class TestHintRows:
    def test_hint_keeps_the_ordering_contract_and_prunes_control(self, tmp_path):
        available = tool_get_raw_file(_crowded_store(tmp_path), MISSING)["available_files"]
        assert available == ORDINARY_HINT
        assert not any(entry.startswith(".") for entry in available)
        assert all("\\" not in entry for entry in available)

    @POSIX_ONLY
    def test_hint_skips_folded_names_and_directory_links(self, tmp_path):
        store = _crowded_store(tmp_path)
        alias = store / " .replica. "
        alias.mkdir()
        (alias / "x.md").write_text("control\n", encoding="utf-8")
        folded = store / " .. "
        folded.mkdir()
        (folded / "x.md").write_text("folded\n", encoding="utf-8")
        (store / "ctx-link").symlink_to(store / "context", target_is_directory=True)

        available = tool_get_raw_file(store, MISSING)["available_files"]
        assert available == ORDINARY_HINT
        assert not any("replica" in entry for entry in available)
        assert not any(entry.startswith(" ") for entry in available)
        assert not any(entry.startswith("ctx-link") for entry in available)

    @pytest.mark.parametrize("name", ["note.md", UPPERCASE_SUFFIX_ROW])
    def test_hint_names_markdown_the_platform_matches(self, tmp_path, name):
        store = tmp_path / "store"
        (store / "context").mkdir(parents=True)
        (store / "project.md").write_text(PROJECT_BODY, encoding="utf-8")
        (store / "context" / name).write_text("note\n", encoding="utf-8")
        assert f"context/{name}" in tool_get_raw_file(store, MISSING)["available_files"]

    def test_hint_never_enumerates_with_rglob(self, tmp_path, monkeypatch):
        store = _crowded_store(tmp_path)

        def forbidden(self, *args, **kwargs):
            pytest.fail(f"rglob {self!r}")

        monkeypatch.setattr(Path, "rglob", forbidden)
        assert tool_get_raw_file(store, MISSING)["available_files"] == ORDINARY_HINT


class TestBackupRows:
    def test_ordinary_backups_are_listed_and_counted(self, tmp_path, monkeypatch):
        store = _status_store(tmp_path, monkeypatch)
        save_quarantine_backup(store, "decisions/003-remote.md", b"remote body\n", '"etag"')
        write_backup(store, "20260810T000000Z-project.md", b"losing side\n")

        quarantined = list_quarantine_backups(store)
        assert [item.remote_path for item in quarantined] == ["decisions/003-remote.md"]
        assert {path.name for path in list_conflict_backup_files(store)} == {
            quarantined[0].backup_path.name,
            "20260810T000000Z-project.md",
        }

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Quarantined decision-number collisions: 1" in result.output
        assert "Conflict backups: 1" in result.output

    @POSIX_ONLY
    def test_dangling_backup_link_is_reported_unlistable(self, tmp_path, monkeypatch):
        store = _status_store(tmp_path, monkeypatch)
        (store / CONFLICT_BACKUP_DIR).symlink_to(tmp_path / "missing-target")
        _assert_unlistable(store)

    def test_missing_backup_directory_lists_nothing(self, tmp_path, monkeypatch):
        store = _status_store(tmp_path, monkeypatch)
        assert list_conflict_backup_files(store) == []
        assert list_quarantine_backups(store) == []

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Conflict backups" not in result.output

    @pytest.mark.parametrize("kind", LINK_KINDS)
    def test_linked_backup_directory_is_reported_unlistable(self, tmp_path, monkeypatch, kind):
        _require_link_kind(kind)
        store = _status_store(tmp_path, monkeypatch)
        save_quarantine_backup(store, "decisions/003-remote.md", b"remote body\n", '"etag"')
        outside = tmp_path / "outside"
        (store / CONFLICT_BACKUP_DIR).rename(outside)
        _link(store / CONFLICT_BACKUP_DIR, outside, kind)
        _forbid_access(monkeypatch, outside, exact=True)
        _assert_unlistable(store)

    @POSIX_ONLY
    def test_unreadable_backup_directory_is_reported_unlistable(self, tmp_path, monkeypatch):
        store = _status_store(tmp_path, monkeypatch)
        save_quarantine_backup(store, "decisions/003-remote.md", b"remote body\n", '"etag"')
        directory = store / CONFLICT_BACKUP_DIR
        directory.chmod(0o000)
        try:
            _assert_unlistable(store)
        finally:
            directory.chmod(0o700)

    def test_backup_path_that_is_a_file_is_reported_unlistable(self, tmp_path, monkeypatch):
        store = _status_store(tmp_path, monkeypatch)
        (store / CONFLICT_BACKUP_DIR).write_text("not a directory", encoding="utf-8")
        _assert_unlistable(store)

    @POSIX_ONLY
    def test_linked_entry_inside_the_directory_is_not_counted(self, tmp_path, monkeypatch):
        store = _status_store(tmp_path, monkeypatch)
        write_backup(store, "20260810T000000Z-project.md", b"losing side\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim.md"
        victim.write_text("victim\n", encoding="utf-8")
        (store / CONFLICT_BACKUP_DIR / "20260811T000000Z-linked.md").symlink_to(victim)

        assert {path.name for path in list_conflict_backup_files(store)} == {
            "20260810T000000Z-project.md"
        }

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Conflict backups: 1" in result.output

    @POSIX_ONLY
    def test_folded_entry_is_skipped_before_its_stat(self, tmp_path, monkeypatch):
        store = _status_store(tmp_path, monkeypatch)
        write_backup(store, "20260810T000000Z-project.md", b"losing side\n")
        directory = store / CONFLICT_BACKUP_DIR
        (directory / " .. ").write_text("trap\n", encoding="utf-8")
        _forbid_entry_stat(monkeypatch, directory, " .. ")

        assert {path.name for path in list_conflict_backup_files(store)} == {
            "20260810T000000Z-project.md"
        }

    def test_orphaned_tmp_sibling_is_still_not_counted(self, tmp_path, monkeypatch):
        from nauro.store import _atomic

        store = _status_store(tmp_path, monkeypatch)
        with patch.object(_atomic.os, "replace", lambda src, dst: None):
            write_backup(store, "20260810T000000Z-project.md", b"losing side\n")
        assert list_conflict_backup_files(store) != []

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Conflict backups" not in result.output
