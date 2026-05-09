"""Tests for the extended ``nauro setup`` (cursor + codex subcommands)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.commands.setup import _configure_codex, _configure_cursor_for_repo
from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

runner = CliRunner()


def _mock_claude_cli(monkeypatch, *, on_path: bool = True, returncode: int = 0):
    """Mock the `claude` CLI for any test that exercises the Claude Code path."""
    monkeypatch.setattr(
        "nauro.cli.commands.setup.shutil.which",
        lambda cmd: "/usr/local/bin/claude" if (on_path and cmd == "claude") else None,
    )
    monkeypatch.setattr(
        "nauro.cli.commands.setup.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout="", stderr=""
        ),
    )


# ─── nauro setup cursor ─────────────────────────────────────────────────────


def test_setup_cursor_writes_repo_mcp_json(tmp_path: Path, monkeypatch):
    """`nauro setup cursor` writes <repo>/.cursor/mcp.json for each project repo."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    pid, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "cursor"])
    assert result.exit_code == 0, result.output

    config_path = repo / ".cursor" / "mcp.json"
    assert config_path.is_file()
    data = json.loads(config_path.read_text())
    assert data["mcpServers"]["nauro"]["args"] == ["serve", "--stdio"]
    assert "command" in data["mcpServers"]["nauro"]


def test_setup_cursor_remove_clears_entry(tmp_path: Path, monkeypatch):
    """`nauro setup cursor --remove` deletes the nauro entry."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    runner.invoke(app, ["setup", "cursor"])
    assert (repo / ".cursor" / "mcp.json").is_file()

    result = runner.invoke(app, ["setup", "cursor", "--remove"])
    assert result.exit_code == 0, result.output
    # Nauro was the only entry, so the now-empty config file is unlinked.
    assert not (repo / ".cursor" / "mcp.json").is_file()


def test_configure_cursor_preserves_other_mcp_servers(tmp_path: Path):
    """Adding nauro to .cursor/mcp.json must not clobber other servers."""
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    config = {"mcpServers": {"other": {"command": "other-cmd", "args": []}}}
    (repo / ".cursor" / "mcp.json").write_text(json.dumps(config))

    _configure_cursor_for_repo(repo, remove=False)

    result = json.loads((repo / ".cursor" / "mcp.json").read_text())
    assert "other" in result["mcpServers"]
    assert "nauro" in result["mcpServers"]


def test_configure_cursor_remove_preserves_other_servers(tmp_path: Path):
    """`--remove` should drop only the nauro entry, not the rest."""
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    config = {
        "mcpServers": {
            "nauro": {"command": "nauro", "args": ["serve", "--stdio"]},
            "other": {"command": "other-cmd", "args": []},
        }
    }
    (repo / ".cursor" / "mcp.json").write_text(json.dumps(config))

    _configure_cursor_for_repo(repo, remove=True)

    result = json.loads((repo / ".cursor" / "mcp.json").read_text())
    assert "other" in result["mcpServers"]
    assert "nauro" not in result["mcpServers"]


# ─── nauro setup codex ──────────────────────────────────────────────────────


def test_setup_codex_writes_config_toml(tmp_path: Path):
    """`_configure_codex` writes ``[mcp_servers.nauro]`` into the target TOML file."""
    config_path = tmp_path / ".codex" / "config.toml"

    msg = _configure_codex(remove=False, config_path=config_path)

    assert "wrote nauro" in msg
    assert config_path.is_file()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    assert data["mcp_servers"]["nauro"]["args"] == ["serve", "--stdio"]


def test_setup_codex_remove_clears_entry(tmp_path: Path):
    config_path = tmp_path / ".codex" / "config.toml"
    _configure_codex(remove=False, config_path=config_path)

    msg = _configure_codex(remove=True, config_path=config_path)

    assert "removed nauro" in msg
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    assert "mcp_servers" not in data or "nauro" not in data.get("mcp_servers", {})


def test_setup_codex_preserves_other_mcp_servers(tmp_path: Path):
    """Adding nauro must not clobber other [mcp_servers.<name>] entries."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('[mcp_servers.other]\ncommand = "other-cmd"\nargs = []\n')

    _configure_codex(remove=False, config_path=config_path)

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    assert "other" in data["mcp_servers"]
    assert "nauro" in data["mcp_servers"]


def test_setup_codex_no_op_when_remove_and_no_entry(tmp_path: Path):
    """`--remove` when no entry exists is idempotent and reports clearly."""
    config_path = tmp_path / ".codex" / "config.toml"
    msg = _configure_codex(remove=True, config_path=config_path)
    assert "no nauro entry to remove" in msg


# ─── claude-code regression ──────────────────────────────────────────────────


def test_setup_claude_code_subcommand_unchanged(tmp_path: Path, monkeypatch):
    """The existing `setup claude-code` command stays callable."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    monkeypatch.setenv("HOME", str(tmp_path))  # divert ~/.claude search
    repo = tmp_path / "myrepo"
    repo.mkdir()
    register_project_v2("myproj", [repo])
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "Configured Nauro" in result.output


def test_setup_top_level_help_lists_new_subcommands():
    """`nauro setup --help` should advertise cursor + codex alongside claude-code."""
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    assert "claude-code" in result.output
    assert "cursor" in result.output
    assert "codex" in result.output
    assert "all" in result.output


# ─── nauro setup all (PR-B2) ─────────────────────────────────────────────────


def test_setup_all_writes_claude_cursor_codex_configs(tmp_path: Path, monkeypatch):
    """`setup all` shells out to `claude mcp add` and writes Cursor + Codex
    configs and skill files across all three surfaces."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)
    _mock_claude_cli(monkeypatch)

    result = runner.invoke(app, ["setup", "all"])
    assert result.exit_code == 0, result.output

    # MCP configs (Claude Code is wired via the mocked `claude mcp add` shellout
    # — the actual .mcp.json write is the `claude` CLI's responsibility, so the
    # multi-repo iteration test in test_setup.py covers the argv shape).
    assert (repo / ".cursor" / "mcp.json").is_file()
    assert (tmp_path / ".codex" / "config.toml").is_file()

    # Skill files (materialized):
    assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "nauro" / "SKILL.md").is_file()
    assert (repo / ".cursor" / "rules" / "nauro-adopt.mdc").is_file()
    assert (repo / ".cursor" / "rules" / "nauro.mdc").is_file()
    assert (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "nauro" / "SKILL.md").is_file()


def test_setup_all_remove_clears_everything(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)
    _mock_claude_cli(monkeypatch)

    runner.invoke(app, ["setup", "all"])
    result = runner.invoke(app, ["setup", "all", "--remove"])
    assert result.exit_code == 0, result.output

    # Skill files gone:
    assert not (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").exists()
    assert not (repo / ".cursor" / "rules" / "nauro-adopt.mdc").exists()
    assert not (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").exists()


def test_remove_skill_file_does_not_walk_above_base(tmp_path: Path):
    """``_remove_skill_file`` must stop at ``stop_above`` — never delete the surface root."""
    from nauro.cli.commands.setup import _remove_skill_file

    base = tmp_path / ".claude" / "skills"
    skill_dir = base / "nauro-adopt"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    target.write_text("body")

    _remove_skill_file(target, stop_above=base)

    assert not target.exists()
    assert not skill_dir.exists()  # subdir was empty → pruned
    assert base.is_dir()  # base preserved
    assert base.parent.is_dir()  # ~/.claude preserved
    assert base.parent.parent.is_dir()  # tmp_path preserved


def test_remove_skill_file_preserves_sibling_skills(tmp_path: Path):
    """If `~/.claude/skills/` has other skills, removing nauro must not touch them."""
    from nauro.cli.commands.setup import _remove_skill_file

    base = tmp_path / ".claude" / "skills"
    nauro_skill = base / "nauro-adopt" / "SKILL.md"
    other_skill = base / "user-other" / "SKILL.md"
    nauro_skill.parent.mkdir(parents=True)
    nauro_skill.write_text("nauro")
    other_skill.parent.mkdir(parents=True)
    other_skill.write_text("user-other")

    _remove_skill_file(nauro_skill, stop_above=base)

    assert not nauro_skill.exists()
    assert not nauro_skill.parent.exists()  # nauro subdir pruned
    assert other_skill.exists()  # other skill untouched
    assert base.is_dir()
