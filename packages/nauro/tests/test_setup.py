"""Tests for nauro setup claude-code command."""

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


def test_inject_into_existing_claude_md(tmp_path: Path, monkeypatch):
    """Injects into existing CLAUDE.md without clobbering other content."""
    repos = _setup_project(tmp_path, monkeypatch)
    existing = "# My Project\n\nSome existing content.\n"
    (repos[0] / "CLAUDE.md").write_text(existing)

    result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    assert result.exit_code == 0

    content = (repos[0] / "CLAUDE.md").read_text()
    assert "# My Project" in content
    assert "Some existing content." in content
    assert CLAUDE_MD_START in content
    assert CLAUDE_MD_END in content
    assert "Nauro — project context" in content


def test_creates_claude_md_if_missing(tmp_path: Path, monkeypatch):
    """Creates CLAUDE.md if it doesn't exist."""
    repos = _setup_project(tmp_path, monkeypatch)
    assert not (repos[0] / "CLAUDE.md").exists()

    result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    assert result.exit_code == 0

    content = (repos[0] / "CLAUDE.md").read_text()
    assert CLAUDE_MD_START in content
    assert "Nauro — project context" in content
    assert "created CLAUDE.md" in result.output


def test_idempotent_rerun(tmp_path: Path, monkeypatch):
    """Re-running replaces existing NAURO block (idempotent)."""
    repos = _setup_project(tmp_path, monkeypatch)
    existing = "# My Project\n\nKeep this.\n"
    (repos[0] / "CLAUDE.md").write_text(existing)

    # Run twice
    runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    assert result.exit_code == 0

    content = (repos[0] / "CLAUDE.md").read_text()
    # Should only have one block
    assert content.count(CLAUDE_MD_START) == 1
    assert content.count(CLAUDE_MD_END) == 1
    assert "# My Project" in content
    assert "Keep this." in content


def test_remove_strips_block(tmp_path: Path, monkeypatch):
    """--remove strips block cleanly, leaves other content intact."""
    repos = _setup_project(tmp_path, monkeypatch)
    existing = "# My Project\n\nKeep this.\n"
    (repos[0] / "CLAUDE.md").write_text(existing)

    # Add then remove
    runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj", "--remove"])
    assert result.exit_code == 0

    content = (repos[0] / "CLAUDE.md").read_text()
    assert CLAUDE_MD_START not in content
    assert CLAUDE_MD_END not in content
    assert "# My Project" in content
    assert "Keep this." in content


def test_remove_deletes_file_if_only_nauro(tmp_path: Path, monkeypatch):
    """--remove on CLAUDE.md with only the Nauro block deletes the file."""
    repos = _setup_project(tmp_path, monkeypatch)

    # Create CLAUDE.md with only Nauro block
    runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    assert (repos[0] / "CLAUDE.md").exists()

    result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj", "--remove"])
    assert result.exit_code == 0
    assert not (repos[0] / "CLAUDE.md").exists()
    assert "deleted CLAUDE.md" in result.output


def test_block_contains_behavioral_instructions(tmp_path: Path, monkeypatch):
    """Block contains behavioral instructions for logging decisions."""
    repos = _setup_project(tmp_path, monkeypatch)

    runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    content = (repos[0] / "CLAUDE.md").read_text()

    assert "propose_decision" in content
    assert "When to propose a decision" in content
    assert "Examples that warrant a decision:" in content


def test_mcp_config_merge_preserves_existing(tmp_path: Path, monkeypatch):
    """MCP config merge preserves existing servers."""
    _setup_project(tmp_path, monkeypatch)

    # Create pre-existing MCP config with another server
    claude_dir = tmp_path / "claude_home"
    claude_dir.mkdir()
    config_path = claude_dir / "claude_desktop_config.json"
    import json

    config_path.write_text(
        json.dumps({"mcpServers": {"other-tool": {"url": "http://localhost:9999"}}})
    )

    # Patch _find_claude_config_path to use our test path
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


def test_mcp_config_created_from_scratch(tmp_path: Path, monkeypatch):
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

    import json

    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["nauro"]["args"] == ["serve", "--stdio"]
    assert "command" in config["mcpServers"]["nauro"]


def test_multi_repo_all_updated(tmp_path: Path, monkeypatch):
    """Multi-repo: all associated repos get updated CLAUDE.md."""
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()

    _setup_project(tmp_path, monkeypatch, repo_paths=[repo1, repo2])

    # Patch MCP config to avoid touching real home
    monkeypatch.setattr(
        "nauro.cli.commands.setup._find_claude_config_path",
        lambda: None,
    )

    result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
    assert result.exit_code == 0

    assert (repo1 / "CLAUDE.md").exists()
    assert (repo2 / "CLAUDE.md").exists()
    assert "Nauro — project context" in (repo1 / "CLAUDE.md").read_text()
    assert "Nauro — project context" in (repo2 / "CLAUDE.md").read_text()


def test_project_flag_overrides_cwd(tmp_path: Path, monkeypatch):
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

    # cwd is in proj-a's repo, but we target proj-b
    monkeypatch.chdir(repo_a)

    monkeypatch.setattr(
        "nauro.cli.commands.setup._find_claude_config_path",
        lambda: None,
    )

    result = runner.invoke(app, ["setup", "claude-code", "--project", "proj-b"])
    assert result.exit_code == 0

    assert (repo_b / "CLAUDE.md").exists()
    assert "Nauro — project context" in (repo_b / "CLAUDE.md").read_text()
    # repo_a should NOT have been touched
    assert not (repo_a / "CLAUDE.md").exists()


def test_setup_regenerates_agents_md(tmp_path: Path, monkeypatch):
    """Setup regenerates AGENTS.md in all repos."""
    repos = _setup_project(tmp_path, monkeypatch)

    # Patch MCP config to avoid touching real home
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


def test_remove_mcp_entry(tmp_path: Path, monkeypatch):
    """--remove removes the nauro MCP entry from config."""
    _setup_project(tmp_path, monkeypatch)

    import json

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
