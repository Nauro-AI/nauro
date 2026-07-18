"""Managed .gitignore block for machine-local wiring files.

Wiring configs record absolute binary paths, so committing them ships a dead
command to every other clone. These tests pin the enforcement layer: entries
are added to a marker-delimited block at write time, already-ignored paths are
never duplicated, tracked files refuse the write, and teardown removes exactly
what Nauro added.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.git_hygiene import (
    GITIGNORE_BLOCK_BEGIN,
    GITIGNORE_BLOCK_END,
    GitIgnoreKind,
    ensure_wiring_ignored,
    remove_wiring_ignore_entry,
    wiring_path_is_tracked,
)
from nauro.cli.main import app

runner = CliRunner()


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    return repo


def _is_effectively_ignored(repo: Path, rel_path: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--", rel_path],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


# ── ensure_wiring_ignored ──────────────────────────────────────────────────────


def test_ensure_creates_block_and_git_honors_it(tmp_path: Path):
    repo = _git_repo(tmp_path)

    result = ensure_wiring_ignored(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.ADDED
    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert GITIGNORE_BLOCK_BEGIN in content
    assert GITIGNORE_BLOCK_END in content
    # Entries anchor to the repo root so they never match in subdirectories.
    assert "/.mcp.json" in content
    assert _is_effectively_ignored(repo, ".mcp.json")


def test_ensure_is_idempotent(tmp_path: Path):
    repo = _git_repo(tmp_path)
    ensure_wiring_ignored(repo, ".mcp.json")

    result = ensure_wiring_ignored(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.ALREADY_COVERED
    assert (repo / ".gitignore").read_text(encoding="utf-8").count("/.mcp.json") == 1


def test_ensure_appends_second_entry_into_existing_block(tmp_path: Path):
    repo = _git_repo(tmp_path)
    ensure_wiring_ignored(repo, ".mcp.json")
    ensure_wiring_ignored(repo, ".cursor/mcp.json")

    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert content.count(GITIGNORE_BLOCK_BEGIN) == 1
    assert content.count(GITIGNORE_BLOCK_END) == 1
    assert "/.mcp.json" in content
    assert "/.cursor/mcp.json" in content
    assert _is_effectively_ignored(repo, ".cursor/mcp.json")


def test_ensure_respects_existing_user_ignore_rule(tmp_path: Path):
    """A path the user already ignores is never duplicated into the block."""
    repo = _git_repo(tmp_path)
    (repo / ".gitignore").write_text(".mcp.json\n", encoding="utf-8")

    result = ensure_wiring_ignored(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.ALREADY_COVERED
    assert GITIGNORE_BLOCK_BEGIN not in (repo / ".gitignore").read_text(encoding="utf-8")


def test_ensure_preserves_user_gitignore_content(tmp_path: Path):
    repo = _git_repo(tmp_path)
    user_content = "node_modules/\n*.log\n"
    (repo / ".gitignore").write_text(user_content, encoding="utf-8")

    ensure_wiring_ignored(repo, ".mcp.json")

    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert content.startswith(user_content)
    assert "/.mcp.json" in content


def test_ensure_skips_non_git_directory(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()

    result = ensure_wiring_ignored(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.SKIPPED_NON_GIT
    assert not (repo / ".gitignore").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_ensure_refuses_symlinked_gitignore(tmp_path: Path):
    repo = _git_repo(tmp_path)
    outside = tmp_path / "outside-gitignore"
    outside.write_text("keep\n", encoding="utf-8")
    (repo / ".gitignore").symlink_to(outside)

    result = ensure_wiring_ignored(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REFUSED_SYMLINK
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert (repo / ".gitignore").is_symlink()


def test_ensure_refuses_orphaned_begin_marker(tmp_path: Path):
    """A begin marker whose end line was hand-deleted refuses rather than
    appending a second block a later removal could over-delete against."""
    repo = _git_repo(tmp_path)
    content = f"{GITIGNORE_BLOCK_BEGIN}\n/.mcp.json\nuser-line\n"
    (repo / ".gitignore").write_text(content, encoding="utf-8")

    result = ensure_wiring_ignored(repo, ".cursor/mcp.json")

    assert result.kind is GitIgnoreKind.REFUSED_MALFORMED_BLOCK
    assert (repo / ".gitignore").read_text(encoding="utf-8") == content


def test_remove_refuses_orphaned_begin_marker(tmp_path: Path):
    repo = _git_repo(tmp_path)
    content = f"{GITIGNORE_BLOCK_BEGIN}\n/.mcp.json\nuser-line\n"
    (repo / ".gitignore").write_text(content, encoding="utf-8")

    result = remove_wiring_ignore_entry(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REFUSED_MALFORMED_BLOCK
    assert (repo / ".gitignore").read_text(encoding="utf-8") == content


def test_ensure_reports_unwritable_gitignore_as_typed_refusal(tmp_path: Path, monkeypatch):
    """A failed .gitignore write degrades to a typed refusal on the outcome,
    never an exception that swallows the codec's own status line."""
    import nauro.cli.git_hygiene as git_hygiene_mod

    repo = _git_repo(tmp_path)

    def failing_write(path: Path, text: str) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(git_hygiene_mod, "atomic_write_text", failing_write)

    result = ensure_wiring_ignored(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REFUSED_UNWRITABLE
    assert "read-only file system" in result.detail
    assert not (repo / ".gitignore").exists()


def test_ensure_refuses_non_utf8_gitignore(tmp_path: Path):
    repo = _git_repo(tmp_path)
    raw = b"\xff\xfe*.log\n"
    (repo / ".gitignore").write_bytes(raw)

    result = ensure_wiring_ignored(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REFUSED_UNREADABLE
    assert (repo / ".gitignore").read_bytes() == raw


# ── remove_wiring_ignore_entry ─────────────────────────────────────────────────


def test_remove_entry_keeps_block_with_remaining_entries(tmp_path: Path):
    repo = _git_repo(tmp_path)
    ensure_wiring_ignored(repo, ".mcp.json")
    ensure_wiring_ignored(repo, ".cursor/mcp.json")

    result = remove_wiring_ignore_entry(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REMOVED_ENTRY
    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "/.mcp.json" not in content
    assert "/.cursor/mcp.json" in content
    assert GITIGNORE_BLOCK_BEGIN in content


def test_remove_last_entry_drops_block_and_preserves_user_content(tmp_path: Path):
    repo = _git_repo(tmp_path)
    user_content = "node_modules/\n"
    (repo / ".gitignore").write_text(user_content, encoding="utf-8")
    ensure_wiring_ignored(repo, ".mcp.json")

    result = remove_wiring_ignore_entry(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REMOVED_BLOCK
    assert (repo / ".gitignore").read_text(encoding="utf-8") == user_content


def test_remove_last_entry_unlinks_gitignore_nauro_created(tmp_path: Path):
    repo = _git_repo(tmp_path)
    ensure_wiring_ignored(repo, ".mcp.json")

    result = remove_wiring_ignore_entry(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REMOVED_BLOCK
    assert not (repo / ".gitignore").exists()


def test_remove_entry_without_block_is_noop(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    result = remove_wiring_ignore_entry(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.NOTHING_TO_REMOVE
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "node_modules/\n"


def test_remove_entry_never_touches_user_rule_outside_block(tmp_path: Path):
    """A user's own rule for the same path lives outside the markers and survives."""
    repo = _git_repo(tmp_path)
    (repo / ".gitignore").write_text(
        f".mcp.json\n{GITIGNORE_BLOCK_BEGIN}\n/.mcp.json\n{GITIGNORE_BLOCK_END}\n",
        encoding="utf-8",
    )

    result = remove_wiring_ignore_entry(repo, ".mcp.json")

    assert result.kind is GitIgnoreKind.REMOVED_BLOCK
    assert (repo / ".gitignore").read_text(encoding="utf-8") == ".mcp.json\n"


# ── wiring_path_is_tracked ─────────────────────────────────────────────────────


def test_wiring_path_is_tracked_detects_staged_file(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / ".mcp.json").write_text("{}\n", encoding="utf-8")
    assert wiring_path_is_tracked(repo, ".mcp.json") is False
    subprocess.run(["git", "add", ".mcp.json"], cwd=repo, check=True)
    assert wiring_path_is_tracked(repo, ".mcp.json") is True


def test_wiring_path_is_tracked_soft_fails_outside_git(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    assert wiring_path_is_tracked(repo, ".mcp.json") is False


# ── adopt / un-adopt symmetry ──────────────────────────────────────────────────


def _adopt_env(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    return repo


def test_adopt_gitignores_wiring_and_unadopt_removes_block(tmp_path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "/.mcp.json" in content
    assert "/.cursor/mcp.json" in content
    assert _is_effectively_ignored(repo, ".mcp.json")
    assert _is_effectively_ignored(repo, ".cursor/mcp.json")
    # Identity surfaces stay committable.
    assert not _is_effectively_ignored(repo, "AGENTS.md")
    assert not _is_effectively_ignored(repo, ".nauro/config.json")
    assert "added .mcp.json to .gitignore" in result.output

    removed = runner.invoke(app, ["adopt", "--remove", "--yes"])
    assert removed.exit_code == 0, removed.output
    # The block was Nauro's only content, so the file is gone entirely.
    assert not (repo / ".gitignore").exists()


def test_unadopt_preserves_user_gitignore_lines(tmp_path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    assert runner.invoke(app, ["adopt", "--name", "alpha"]).exit_code == 0
    removed = runner.invoke(app, ["adopt", "--remove", "--yes"])
    assert removed.exit_code == 0, removed.output

    assert (repo / ".gitignore").read_text(encoding="utf-8") == "node_modules/\n"


def test_adopt_refuses_tracked_wiring_but_completes(tmp_path, monkeypatch):
    """A tracked wiring file refuses only that surface; adoption itself lands."""
    repo = _adopt_env(monkeypatch, tmp_path)
    (repo / ".mcp.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "add", ".mcp.json"], cwd=repo, check=True)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])

    assert result.exit_code == 0, result.output
    assert ".mcp.json is tracked by git - skipped writing" in result.output
    assert "git rm --cached .mcp.json" in result.output
    # The tracked file was never written; the other wiring surface proceeded.
    assert (repo / ".mcp.json").read_text(encoding="utf-8") == "{}\n"
    cursor_config = json.loads((repo / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "nauro" in cursor_config["mcpServers"]
    assert (repo / ".nauro" / "config.json").is_file()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_unadopt_aborts_on_symlinked_gitignore_in_git_repo(tmp_path, monkeypatch):
    """Inside a git tree the teardown mutates .gitignore, so a planted link
    there halts un-adopt with wiring and registry intact."""
    repo = _adopt_env(monkeypatch, tmp_path)
    assert runner.invoke(app, ["adopt", "--name", "alpha"]).exit_code == 0
    (repo / ".gitignore").unlink()
    outside = tmp_path / "outside-gitignore"
    outside.write_text("keep\n", encoding="utf-8")
    (repo / ".gitignore").symlink_to(outside)

    result = runner.invoke(app, ["adopt", "--remove", "--yes"])

    assert result.exit_code == 1
    assert "Un-adopt aborted" in result.output
    assert (repo / ".nauro" / "config.json").is_file()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_unadopt_proceeds_with_symlinked_gitignore_outside_git(tmp_path, monkeypatch):
    """Without a git tree no codec touches .gitignore, so a symlinked one
    must not abort the teardown."""
    repo = _adopt_env(monkeypatch, tmp_path)
    assert runner.invoke(app, ["adopt", "--name", "alpha"]).exit_code == 0
    (repo / ".gitignore").unlink()
    outside = tmp_path / "outside-gitignore"
    outside.write_text("keep\n", encoding="utf-8")
    (repo / ".gitignore").symlink_to(outside)
    import shutil

    shutil.rmtree(repo / ".git")

    result = runner.invoke(app, ["adopt", "--remove", "--yes"])

    assert result.exit_code == 0, result.output
    assert not (repo / ".nauro" / "config.json").exists()
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert (repo / ".gitignore").is_symlink()


def test_single_surface_remove_drops_only_its_own_entry(tmp_path, monkeypatch):
    """`setup cursor --remove` keeps the .mcp.json entry other surfaces still need."""
    repo = _adopt_env(monkeypatch, tmp_path)
    assert runner.invoke(app, ["adopt", "--name", "alpha"]).exit_code == 0

    result = runner.invoke(app, ["setup", "cursor", "--remove"])
    assert result.exit_code == 0, result.output

    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "/.cursor/mcp.json" not in content
    assert "/.mcp.json" in content
