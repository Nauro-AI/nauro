"""Tests for ``nauro adopt`` (PR-B2)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.skills import load_adopt_body
from nauro.store.registry import find_projects_by_name_v2, register_project_v2

runner = CliRunner()


def _adopt_env(monkeypatch, tmp_path: Path) -> Path:
    """Set up an isolated NAURO_HOME + HOME for adopt tests."""
    monkeypatch.setenv("HOME", str(tmp_path))  # diverts ~/.claude, ~/.codex, ~/.agents
    repo = tmp_path / "myrepo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    return repo


def test_adopt_creates_v2_project_and_writes_repo_config(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    # Per-repo config written.
    config_path = repo / ".nauro" / "config.json"
    assert config_path.is_file()
    data = json.loads(config_path.read_text())
    assert data["name"] == "alpha"
    assert data["mode"] == "local"
    assert "id" in data

    # v2 registry has the entry.
    matches = find_projects_by_name_v2("alpha")
    assert len(matches) == 1
    pid, entry = matches[0]
    assert str(repo) in entry["repo_paths"]
    assert pid == data["id"]


def test_adopt_uses_repo_basename_when_name_omitted(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt"])
    assert result.exit_code == 0, result.output

    data = json.loads((repo / ".nauro" / "config.json").read_text())
    assert data["name"] == "myrepo"


def test_adopt_aborts_when_repo_already_adopted(tmp_path: Path, monkeypatch):
    _adopt_env(monkeypatch, tmp_path)
    runner.invoke(app, ["adopt", "--name", "alpha"])

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code != 0
    assert "already adopted" in result.output.lower()


def test_adopt_aborts_on_same_name_collision(tmp_path: Path, monkeypatch):
    """Pre-check fires when the v2 registry has a same-name project at a different repo."""
    monkeypatch.setenv("HOME", str(tmp_path))
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    register_project_v2("alpha", [other_repo])

    new_repo = tmp_path / "alpha"
    new_repo.mkdir()
    monkeypatch.chdir(new_repo)

    result = runner.invoke(app, ["adopt"])  # infers name="alpha", collides
    assert result.exit_code != 0
    assert "A project named 'alpha' already exists" in result.output
    assert "--name <unique-name>" in result.output
    assert "nauro attach" in result.output
    assert "nauro link" in result.output


def test_collision_message_picks_first_repo_deterministically(tmp_path: Path, monkeypatch):
    """When the colliding project has multiple registered repos, the surfaced path is stable."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_c = tmp_path / "repo-c"
    for r in (repo_a, repo_b, repo_c):
        r.mkdir()
    # Register colliding project with three repos in a known order.
    register_project_v2("alpha", [repo_a, repo_b, repo_c])

    new_repo = tmp_path / "alpha"
    new_repo.mkdir()
    monkeypatch.chdir(new_repo)

    # Run twice — same surfaced path each time (deterministic).
    out1 = runner.invoke(app, ["adopt"]).output
    out2 = runner.invoke(app, ["adopt"]).output
    assert out1 == out2
    # And the first registered repo (repo-a) is the one named.
    assert str(repo_a.resolve()) in out1


def test_adopt_print_prompt_outputs_canonical_body(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["adopt", "--print-prompt"])
    assert result.exit_code == 0
    # Output equals the canonical body (Typer's runner adds a trailing newline; we used nl=False).
    assert result.output == load_adopt_body()


def test_adopt_print_prompt_conflicts_with_other_flags(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["adopt", "--print-prompt", "--name", "x"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_adopt_no_setup_and_skills_skips_wiring(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--no-setup-and-skills"])
    assert result.exit_code == 0, result.output

    # Per-repo config still written...
    assert (repo / ".nauro" / "config.json").is_file()
    # ...but no skill files materialized.
    assert not (Path(tmp_path) / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").exists()
    assert not (repo / ".cursor" / "rules" / "nauro-adopt.mdc").exists()
    assert not (Path(tmp_path) / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").exists()


def test_adopt_materializes_skills_across_surfaces(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    # Claude Code: user-global
    assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "nauro" / "SKILL.md").is_file()
    # Cursor: per-project (in target repo)
    assert (repo / ".cursor" / "rules" / "nauro-adopt.mdc").is_file()
    assert (repo / ".cursor" / "rules" / "nauro.mdc").is_file()
    # Codex: user-global
    assert (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "nauro" / "SKILL.md").is_file()


def test_adopt_aborts_on_nonexistent_repo(tmp_path: Path, monkeypatch):

    result = runner.invoke(app, ["adopt", "--repo", str(tmp_path / "missing")])
    assert result.exit_code != 0
    assert "not a directory" in result.output


def test_adopt_top_level_cli_help_lists_adopt():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "adopt" in result.output
