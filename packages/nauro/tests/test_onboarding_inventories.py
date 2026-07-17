"""Filesystem inventory pins for the onboarding commands.

Pins the full set of files each onboarding flow (``adopt``, ``init``,
``attach``) leaves behind across the repo and the fake home directory, ahead
of the internal restructuring of the setup module. Inventories are asserted
with set equality over ``snapshot_tree`` plus ordered section-marker checks
on the output; full transcripts are not pinned here because ``adopt``
requires a real git repo, whose hygiene notes vary with the environment.

The registry and its lock are the only user-home bookkeeping files created by
local onboarding when no authentication config already exists.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.sync import cloud_projects
from tests.conftest import seed_auth_config, snapshot_tree

runner = CliRunner()

EXAMPLE_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"

# Written by every flow below: the registry and its filelock sibling, both
# under NAURO_HOME (tmp_path here).
BOOKKEEPING = {"registry.json", "registry.lock"}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    """Keep user-scoped artifact writes inside the test directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def _make_git_repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    return repo


def _repo_pid(repo: Path) -> str:
    return json.loads((repo / ".nauro" / "config.json").read_text(encoding="utf-8"))["id"]


def _store_files(pid: str) -> set[str]:
    """The scaffolded store files for a project id, relative to NAURO_HOME."""
    return {
        f"projects/{pid}/decisions/001-initial-setup.md",
        f"projects/{pid}/open-questions.md",
        f"projects/{pid}/project.md",
        f"projects/{pid}/stack.md",
        f"projects/{pid}/state_current.md",
    }


def _assert_markers_in_order(text: str, markers: list[str]) -> None:
    pos = 0
    for marker in markers:
        idx = text.find(marker, pos)
        assert idx != -1, f"marker missing or out of order: {marker!r}\n---\n{text}"
        pos = idx + len(marker)


def test_adopt_default_inventory(tmp_path: Path, monkeypatch):
    repo = _make_git_repo(tmp_path, monkeypatch)

    result = runner.invoke(app, ["adopt", "--name", "proj"])

    assert result.exit_code == 0
    pid = _repo_pid(repo)
    assert snapshot_tree(tmp_path) == sorted(
        BOOKKEEPING
        | _store_files(pid)
        | {
            ".agents/skills/nauro-adopt/SKILL.md",
            ".claude/skills/nauro-adopt/SKILL.md",
            ".codex/config.toml",
            "repo/.cursor/mcp.json",
            "repo/.cursor/rules/nauro-adopt.mdc",
            "repo/.mcp.json",
            "repo/.nauro/config.json",
            "repo/AGENTS.md",
        }
    )
    _assert_markers_in_order(
        result.stdout,
        [
            "Adopted project 'proj' (id: ",
            "Wiring MCP and installing skills across surfaces:",
            "wrote nauro to .mcp.json",
            "wrote nauro to .cursor/mcp.json",
            "Codex: wrote nauro to ",
            "regenerated AGENTS.md",
            "Next: restart your agent and invoke /nauro-adopt",
        ],
    )


def test_adopt_with_skills_and_subagents_inventory(tmp_path: Path, monkeypatch):
    repo = _make_git_repo(tmp_path, monkeypatch)

    result = runner.invoke(app, ["adopt", "--name", "proj", "--with-skills", "--with-subagents"])

    assert result.exit_code == 0
    pid = _repo_pid(repo)
    assert snapshot_tree(tmp_path) == sorted(
        BOOKKEEPING
        | _store_files(pid)
        | {
            ".agents/skills/nauro-adopt/SKILL.md",
            ".agents/skills/nauro-context/SKILL.md",
            ".agents/skills/nauro-loop/SKILL.md",
            ".agents/skills/nauro-ship-task/SKILL.md",
            ".claude/agents/nauro-executor.md",
            ".claude/agents/nauro-planner.md",
            ".claude/agents/nauro-reviewer.md",
            ".claude/agents/nauro-tech-lead.md",
            ".claude/skills/nauro-adopt/SKILL.md",
            ".claude/skills/nauro-context/SKILL.md",
            ".claude/skills/nauro-loop/SKILL.md",
            ".claude/skills/nauro-ship-task/SKILL.md",
            ".codex/config.toml",
            "repo/.cursor/mcp.json",
            "repo/.cursor/rules/nauro-adopt.mdc",
            "repo/.cursor/rules/nauro-context.mdc",
            "repo/.cursor/rules/nauro-loop.mdc",
            "repo/.cursor/rules/nauro-ship-task.mdc",
            "repo/.mcp.json",
            "repo/.nauro/config.json",
            "repo/AGENTS.md",
        }
    )
    _assert_markers_in_order(
        result.stdout,
        [
            "Adopted project 'proj' (id: ",
            "Wiring MCP and installing skills across surfaces:",
            "wrote nauro to .mcp.json",
            "installed ",
            "Cloud users: name the remote MCP connector exactly `Nauro`",
            "Next: restart your agent and invoke /nauro-adopt",
        ],
    )


def test_adopt_no_setup_and_skills_inventory(tmp_path: Path, monkeypatch):
    repo = _make_git_repo(tmp_path, monkeypatch)

    result = runner.invoke(app, ["adopt", "--name", "proj", "--no-setup-and-skills"])

    assert result.exit_code == 0
    pid = _repo_pid(repo)
    # Registration + store only: no surface wiring and no AGENTS.md.
    assert snapshot_tree(tmp_path) == sorted(
        BOOKKEEPING | _store_files(pid) | {"repo/.nauro/config.json"}
    )
    _assert_markers_in_order(
        result.stdout,
        [
            "Adopted project 'proj' (id: ",
            "Next: restart your agent and invoke /nauro-adopt",
        ],
    )
    assert "Wiring MCP and installing skills across surfaces:" not in result.stdout


def test_adopt_remove_round_trip_inventory(tmp_path: Path, monkeypatch):
    """Un-adopt inverts adoption up to the pinned residue.

    The residue: the store (left intact by design), the registry, and
    ``~/.codex/config.toml``, whose nauro entry is removed while the emptied
    file survives teardown.
    """
    repo = _make_git_repo(tmp_path, monkeypatch)
    pre = snapshot_tree(tmp_path)

    adopt = runner.invoke(app, ["adopt", "--name", "proj"])
    assert adopt.exit_code == 0
    pid = _repo_pid(repo)

    result = runner.invoke(app, ["adopt", "--remove", "--yes"])

    assert result.exit_code == 0
    assert snapshot_tree(tmp_path) == sorted(
        set(pre) | BOOKKEEPING | _store_files(pid) | {".codex/config.toml"}
    )
    _assert_markers_in_order(
        result.stdout,
        [
            "Removing Nauro integration across surfaces:",
            "removed nauro from .mcp.json",
            "removed nauro from .cursor/mcp.json",
            "Codex: removed nauro from ",
            "removed generated AGENTS.md",
            f"removed project registry entry {pid}",
            "store left intact: ",
            "Done. Restart your agent so it drops the Nauro MCP server.",
        ],
    )


def test_init_with_repo_association_inventory(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(app, ["init", "myproj", "--add-repo", str(repo)])

    assert result.exit_code == 0
    pid = _repo_pid(repo)
    # init registers and scaffolds but wires no surfaces; AGENTS.md is the
    # only repo artifact beyond the per-repo config.
    assert snapshot_tree(tmp_path) == sorted(
        BOOKKEEPING | _store_files(pid) | {"repo/.nauro/config.json", "repo/AGENTS.md"}
    )
    _assert_markers_in_order(
        result.stdout,
        [
            "Initialized project 'myproj'",
            "  Project id: ",
            "  Store: ",
            "  Repo:  ",
            "Next: run 'nauro setup claude-code' to connect your agent",
        ],
    )


def test_attach_happy_path_inventory(tmp_path: Path, monkeypatch):
    """Cloud attach writes repo config + AGENTS.md; the store stays empty.

    Unlike init/adopt, attach does not scaffold the store: the directory is
    created empty (files arrive via sync), so nothing under ``projects/``
    shows up in the inventory.
    """
    seed_auth_config()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        return httpx.Response(
            200,
            json=[
                {
                    "project_id": EXAMPLE_PID,
                    "name": "team-proj",
                    "role": "viewer",
                    "created_at": "2026-04-27T00:00:00Z",
                }
            ],
            request=httpx.Request(method, url),
        )

    with patch.object(cloud_projects.httpx, "request", side_effect=handler):
        result = runner.invoke(app, ["attach", EXAMPLE_PID])

    assert result.exit_code == 0
    assert snapshot_tree(tmp_path) == sorted(
        BOOKKEEPING | {"config.json", "repo/.nauro/config.json", "repo/AGENTS.md"}
    )
    _assert_markers_in_order(
        result.stdout,
        [
            "Attached 'team-proj' to ",
            f"  Project id: {EXAMPLE_PID}",
            "  Store: ",
        ],
    )


def test_adopt_remove_last_repo_keeps_user_scope_for_other_project(tmp_path: Path, monkeypatch):
    """Removing a project's last repo preserves user-scope artifacts while a
    second project remains registered.

    The gate is registry-wide, not project-scoped: the other project never
    wired any surface, yet its bare registry entry is enough to preserve the
    shared skills and the codex entry.
    """
    repo = _make_git_repo(tmp_path, monkeypatch)
    adopt = runner.invoke(app, ["adopt", "--name", "proj"])
    assert adopt.exit_code == 0
    pid = _repo_pid(repo)

    other = tmp_path / "other"
    other.mkdir()
    register_project_v2("other", [other])

    result = runner.invoke(app, ["adopt", "--remove", "--yes"])

    assert result.exit_code == 0
    assert snapshot_tree(tmp_path) == sorted(
        BOOKKEEPING
        | _store_files(pid)
        | {
            ".agents/skills/nauro-adopt/SKILL.md",
            ".claude/skills/nauro-adopt/SKILL.md",
            ".codex/config.toml",
        }
    )
    # The preserved codex file still carries the nauro entry, not an
    # emptied table.
    assert "[mcp_servers.nauro]" in (tmp_path / ".codex" / "config.toml").read_text(
        encoding="utf-8"
    )
    _assert_markers_in_order(
        result.stdout,
        [
            "Removing Nauro integration across surfaces:",
            "preserved ~/.claude/skills/nauro-* (other nauro projects still registered)",
            "preserved ~/.claude/agents/nauro-* (other nauro projects still registered)",
            "Codex: preserved nauro entry in ",
            "preserved ~/.agents/skills/nauro-* (other nauro projects still registered)",
            f"removed project registry entry {pid}",
        ],
    )
