"""Tests for nauro setup claude-code command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.commands.setup import CLAUDE_MD_END, CLAUDE_MD_START
from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _setup_project(tmp_path: Path, monkeypatch, repo_paths: list[Path] | None = None):
    """Helper to create a project with repos."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    if repo_paths is None:
        repo_paths = [tmp_path / "repo"]
        repo_paths[0].mkdir()
    store = register_project("testproj", repo_paths)
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(repo_paths[0])
    return repo_paths


class TestMCPConfig:
    def test_mcp_config_merge_preserves_existing(self, tmp_path: Path, monkeypatch):
        """MCP config merge preserves existing servers."""
        _setup_project(tmp_path, monkeypatch)

        # Create pre-existing MCP config with another server
        claude_dir = tmp_path / "claude_home"
        claude_dir.mkdir()
        config_path = claude_dir / "claude_desktop_config.json"
        config_path.write_text(
            json.dumps({"mcpServers": {"other-tool": {"url": "http://localhost:9999"}}})
        )

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: config_path,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0

        config = json.loads(config_path.read_text())
        assert "nauro" in config["mcpServers"]
        assert config["mcpServers"]["nauro"]["args"] == ["serve", "--stdio"]
        assert "command" in config["mcpServers"]["nauro"]
        # Existing server preserved
        assert "other-tool" in config["mcpServers"]
        assert config["mcpServers"]["other-tool"]["url"] == "http://localhost:9999"

    def test_mcp_config_created_from_scratch(self, tmp_path: Path, monkeypatch):
        """MCP config created from scratch if missing."""
        _setup_project(tmp_path, monkeypatch)

        config_path = tmp_path / "claude_home" / "claude_desktop_config.json"
        (tmp_path / "claude_home").mkdir()

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: config_path,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0
        assert config_path.exists()

        config = json.loads(config_path.read_text())
        assert config["mcpServers"]["nauro"]["args"] == ["serve", "--stdio"]
        assert "command" in config["mcpServers"]["nauro"]

    def test_remove_mcp_entry(self, tmp_path: Path, monkeypatch):
        """--remove removes the nauro MCP entry from config."""
        _setup_project(tmp_path, monkeypatch)

        config_path = tmp_path / "claude_home" / "claude_desktop_config.json"
        (tmp_path / "claude_home").mkdir()
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "nauro": {"url": "http://127.0.0.1:7432"},
                        "other": {"url": "http://localhost:8000"},
                    }
                }
            )
        )

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: config_path,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj", "--remove"])
        assert result.exit_code == 0

        config = json.loads(config_path.read_text())
        assert "nauro" not in config["mcpServers"]
        assert "other" in config["mcpServers"]


class TestAGENTSMD:
    def test_setup_regenerates_agents_md(self, tmp_path: Path, monkeypatch):
        """Setup regenerates AGENTS.md in all repos."""
        repos = _setup_project(tmp_path, monkeypatch)

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: None,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0

        agents_md = repos[0] / "AGENTS.md"
        assert agents_md.exists()
        content = agents_md.read_text()
        assert "## Project: testproj" in content
        assert "When to use these tools" in content
        assert "regenerated AGENTS.md" in result.output


class TestNoClaudeMDInjection:
    def test_setup_does_not_create_claude_md(self, tmp_path: Path, monkeypatch):
        """Setup no longer creates or modifies CLAUDE.md."""
        repos = _setup_project(tmp_path, monkeypatch)
        assert not (repos[0] / "CLAUDE.md").exists()

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: None,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0
        assert not (repos[0] / "CLAUDE.md").exists()

    def test_setup_does_not_modify_existing_claude_md(self, tmp_path: Path, monkeypatch):
        """Setup leaves existing CLAUDE.md untouched (no injection)."""
        repos = _setup_project(tmp_path, monkeypatch)
        existing = "# My Project\n\nSome existing content.\n"
        (repos[0] / "CLAUDE.md").write_text(existing)

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: None,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0

        content = (repos[0] / "CLAUDE.md").read_text()
        assert content == existing


class TestLegacyCleanup:
    def test_removes_legacy_block_from_claude_md(self, tmp_path: Path, monkeypatch):
        """Setup removes legacy Nauro block from CLAUDE.md."""
        repos = _setup_project(tmp_path, monkeypatch)
        content = f"# My Project\n\nKeep this.\n\n{CLAUDE_MD_START}\nold block\n{CLAUDE_MD_END}\n"
        (repos[0] / "CLAUDE.md").write_text(content)

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: None,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0

        cleaned = (repos[0] / "CLAUDE.md").read_text()
        assert CLAUDE_MD_START not in cleaned
        assert "# My Project" in cleaned
        assert "Keep this." in cleaned
        assert "Legacy cleanup" in result.output

    def test_deletes_claude_md_if_only_legacy_block(self, tmp_path: Path, monkeypatch):
        """Deletes CLAUDE.md if it only contained the legacy Nauro block."""
        repos = _setup_project(tmp_path, monkeypatch)
        content = f"{CLAUDE_MD_START}\nold block\n{CLAUDE_MD_END}\n"
        (repos[0] / "CLAUDE.md").write_text(content)

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: None,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0
        assert not (repos[0] / "CLAUDE.md").exists()


class TestProjectResolution:
    def test_project_flag_overrides_cwd(self, tmp_path: Path, monkeypatch):
        """--project flag overrides cwd resolution."""
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))

        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        store_a = register_project("proj-a", [repo_a])
        scaffold_project_store("proj-a", store_a)
        store_b = register_project("proj-b", [repo_b])
        scaffold_project_store("proj-b", store_b)

        monkeypatch.chdir(repo_a)

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: None,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "proj-b"])
        assert result.exit_code == 0

        # AGENTS.md should be generated in proj-b's repo
        assert (repo_b / "AGENTS.md").exists()

    def test_multi_repo_all_get_agents_md(self, tmp_path: Path, monkeypatch):
        """Multi-repo: all associated repos get AGENTS.md."""
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()

        _setup_project(tmp_path, monkeypatch, repo_paths=[repo1, repo2])

        monkeypatch.setattr(
            "nauro.cli.commands.setup._find_claude_config_path",
            lambda: None,
        )

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0

        assert (repo1 / "AGENTS.md").exists()
        assert (repo2 / "AGENTS.md").exists()
        # No CLAUDE.md created
        assert not (repo1 / "CLAUDE.md").exists()
        assert not (repo2 / "CLAUDE.md").exists()
