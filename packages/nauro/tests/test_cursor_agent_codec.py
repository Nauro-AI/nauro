"""Focused tests for the repository-scoped Cursor workflow-agent codec."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nauro.agents import AGENT_NAMES, render_agent
from nauro.cli.integrations.agents import (
    materialize_agents,
    materialize_agents_cursor_for_repo,
)
from nauro.cli.integrations.outcomes import AgentKind

symlinks_required = pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra Windows privileges"
)


def _target(repo: Path, name: str) -> Path:
    return repo / ".cursor" / "agents" / f"{name}.md"


def test_cursor_materializer_installs_exact_files_and_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    installed = materialize_agents_cursor_for_repo(repo, remove=False)

    assert [outcome.kind for outcome in installed] == [AgentKind.INSTALLED] * len(AGENT_NAMES)
    for name in AGENT_NAMES:
        assert _target(repo, name).read_text(encoding="utf-8") == render_agent("cursor", name)

    unchanged = materialize_agents_cursor_for_repo(repo, remove=False)

    assert [outcome.kind for outcome in unchanged] == [AgentKind.UNCHANGED] * len(AGENT_NAMES)


def test_cursor_materializer_backs_up_a_differing_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = _target(repo, AGENT_NAMES[0])
    target.parent.mkdir(parents=True)
    target.write_text("local instructions\n", encoding="utf-8")

    outcomes = materialize_agents_cursor_for_repo(repo, remove=False)

    assert outcomes[0].kind is AgentKind.UPDATED
    assert outcomes[0].backup_name == f"{AGENT_NAMES[0]}.md.bak"
    assert target.read_text(encoding="utf-8") == render_agent("cursor", AGENT_NAMES[0])
    assert target.with_name(target.name + ".bak").read_text(encoding="utf-8") == (
        "local instructions\n"
    )


def test_cursor_materializer_force_overwrites_without_touching_backup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = _target(repo, AGENT_NAMES[0])
    target.parent.mkdir(parents=True)
    target.write_text("local instructions\n", encoding="utf-8")
    backup = target.with_name(target.name + ".bak")
    backup.write_text("older backup\n", encoding="utf-8")

    outcomes = materialize_agents_cursor_for_repo(
        repo,
        remove=False,
        force_overwrite=True,
    )

    assert outcomes[0].kind is AgentKind.OVERWROTE
    assert target.read_text(encoding="utf-8") == render_agent("cursor", AGENT_NAMES[0])
    assert backup.read_text(encoding="utf-8") == "older backup\n"


def test_cursor_materializer_remove_preserves_modified_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize_agents_cursor_for_repo(repo, remove=False)
    modified = _target(repo, "nauro-executor")
    modified.write_text("local executor\n", encoding="utf-8")

    outcomes = materialize_agents_cursor_for_repo(repo, remove=True)

    by_target = {outcome.target: outcome.kind for outcome in outcomes}
    assert by_target[modified] is AgentKind.PRESERVED_MODIFIED
    assert modified.read_text(encoding="utf-8") == "local executor\n"
    for name in AGENT_NAMES:
        target = _target(repo, name)
        if target != modified:
            assert by_target[target] is AgentKind.REMOVED
            assert not target.exists()


@symlinks_required
def test_cursor_materializer_refuses_symlinked_parent_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (repo / ".cursor").symlink_to(external, target_is_directory=True)

    outcomes = materialize_agents_cursor_for_repo(repo, remove=False)

    assert [outcome.kind for outcome in outcomes] == [AgentKind.REFUSED_SYMLINK] * len(AGENT_NAMES)
    assert all(outcome.refusal.link == repo / ".cursor" for outcome in outcomes)
    assert not (external / "agents").exists()


@symlinks_required
def test_cursor_materializer_refuses_symlinked_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = _target(repo, AGENT_NAMES[0])
    target.parent.mkdir(parents=True)
    external = tmp_path / "external-agent.md"
    external.write_text("external\n", encoding="utf-8")
    target.symlink_to(external)

    outcomes = materialize_agents_cursor_for_repo(repo, remove=False)

    assert outcomes[0].kind is AgentKind.REFUSED_SYMLINK
    assert outcomes[0].refusal.target == target
    assert outcomes[0].refusal.link == target
    assert external.read_text(encoding="utf-8") == "external\n"
    assert all(_target(repo, name).is_file() for name in AGENT_NAMES[1:])


@symlinks_required
def test_cursor_materializer_refuses_symlinked_backup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = _target(repo, AGENT_NAMES[0])
    target.parent.mkdir(parents=True)
    target.write_text("local instructions\n", encoding="utf-8")
    external = tmp_path / "external-backup.md"
    external.write_text("external\n", encoding="utf-8")
    backup = target.with_name(target.name + ".bak")
    backup.symlink_to(external)

    outcomes = materialize_agents_cursor_for_repo(repo, remove=False)

    assert outcomes[0].kind is AgentKind.REFUSED_SYMLINK
    assert outcomes[0].refusal.target == backup
    assert outcomes[0].refusal.link == backup
    assert target.read_text(encoding="utf-8") == "local instructions\n"
    assert external.read_text(encoding="utf-8") == "external\n"


def test_user_scope_materializer_does_not_accept_cursor() -> None:
    outcomes = materialize_agents("cursor", remove=False)

    assert len(outcomes) == 1
    assert outcomes[0].kind is AgentKind.SURFACE_INVALID
    assert outcomes[0].surface == "cursor"
