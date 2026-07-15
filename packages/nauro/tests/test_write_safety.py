"""Tests for the pure symlink-refusal module (``nauro.store.write_safety``).

Repo-scoped mutations never traverse a symlink, whether the link is the final
file or a directory component on the way to it. Missing paths and regular
files are safe.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nauro.store.write_safety import SymlinkRefusal, find_file_symlink, find_symlink

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra Windows privileges"
)


def test_final_file_symlink_is_refused(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (repo / ".mcp.json").symlink_to(outside)

    refusal = find_symlink(repo, ".mcp.json")

    assert refusal is not None
    assert refusal.target == repo / ".mcp.json"
    assert refusal.link == repo / ".mcp.json"


def test_directory_component_symlink_is_refused(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (repo / ".cursor").symlink_to(outside)

    refusal = find_symlink(repo, ".cursor/mcp.json")

    assert refusal is not None
    assert refusal.target == repo / ".cursor" / "mcp.json"
    assert refusal.link == repo / ".cursor"


def test_nested_component_symlink_is_refused(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (repo / ".cursor" / "rules").symlink_to(outside)

    refusal = find_symlink(repo, ".cursor/rules/nauro-adopt.mdc")

    assert refusal is not None
    assert refusal.link == repo / ".cursor" / "rules"


def test_first_symlink_component_wins(tmp_path: Path):
    """When several components are links, the refusal names the first one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    (outside / "rules").mkdir(parents=True)
    (repo / ".cursor").symlink_to(outside)

    refusal = find_symlink(repo, ".cursor/rules/nauro-adopt.mdc")

    assert refusal is not None
    assert refusal.link == repo / ".cursor"


def test_missing_path_is_safe(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    assert find_symlink(repo, ".nauro/config.json") is None


def test_regular_files_are_safe(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".nauro").mkdir(parents=True)
    (repo / ".nauro" / "config.json").write_text("{}")

    assert find_symlink(repo, ".nauro/config.json") is None


def test_dangling_final_symlink_is_refused(tmp_path: Path):
    """A link to a nonexistent target is still a link and is still refused."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").symlink_to(tmp_path / "does-not-exist")

    refusal = find_symlink(repo, "AGENTS.md")

    assert refusal is not None
    assert refusal.link == repo / "AGENTS.md"


def test_message_when_target_is_the_link(tmp_path: Path):
    target = tmp_path / "repo" / "AGENTS.md"
    refusal = SymlinkRefusal(target=target, link=target)

    assert refusal.message == (
        f"refused to modify {target}: it is a symlink; "
        "Nauro does not write through symlinks in a repo checkout"
    )


def test_message_when_a_component_is_the_link(tmp_path: Path):
    target = tmp_path / "repo" / ".cursor" / "mcp.json"
    link = tmp_path / "repo" / ".cursor"
    refusal = SymlinkRefusal(target=target, link=link)

    assert refusal.message == (
        f"refused to modify {target}: {link} is a symlink; "
        "Nauro does not write through symlinks in a repo checkout"
    )


# ─── find_file_symlink (user-global final-target rule) ──────────────────────


def test_file_symlink_regular_file_is_safe(tmp_path: Path):
    target = tmp_path / "config.toml"
    target.write_text("x")

    assert find_file_symlink(target) is None


def test_file_symlink_final_target_is_refused(tmp_path: Path):
    real = tmp_path / "real.toml"
    real.write_text("x")
    link = tmp_path / "config.toml"
    link.symlink_to(real)

    refusal = find_file_symlink(link)

    assert refusal is not None
    assert refusal.target == link
    assert refusal.message == (
        f"refused to modify {link}: it is a symlink; "
        "Nauro does not replace symlinked user files "
        "(a dotfile manager may own the real file)"
    )


def test_file_symlink_missing_path_is_safe(tmp_path: Path):
    assert find_file_symlink(tmp_path / "absent.toml") is None


def test_file_symlink_symlinked_parent_is_safe(tmp_path: Path):
    """Only the final component is checked: a dotfile-managed parent directory
    (a symlinked ~/.codex, ~/.claude, ...) stays writable."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / ".codex"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    target = link_dir / "config.toml"
    target.write_text("x")

    assert find_file_symlink(target) is None
