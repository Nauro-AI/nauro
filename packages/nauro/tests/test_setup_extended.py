"""Tests for the extended ``nauro setup`` (cursor + codex subcommands)."""

from __future__ import annotations

import json
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
