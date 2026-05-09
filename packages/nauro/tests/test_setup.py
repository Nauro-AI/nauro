"""Tests for nauro setup claude-code command."""

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.commands.setup import CLAUDE_MD_END, CLAUDE_MD_START, _configure_mcp
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


def _mock_claude_cli(monkeypatch, *, on_path: bool = True, returncode: int = 0, stderr: str = ""):
    """Mock the `claude` CLI for setup tests.

    Returns a list that captures every (argv, kwargs) call to subprocess.run
    so individual tests can assert on shape (cwd, scope, etc.).
    """
    calls: list[tuple[list[str], dict]] = []

    monkeypatch.setattr(
        "nauro.cli.commands.setup.shutil.which",
        lambda cmd: "/usr/local/bin/claude" if (on_path and cmd == "claude") else None,
    )

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout="", stderr=stderr
        )

    monkeypatch.setattr("nauro.cli.commands.setup.subprocess.run", fake_run)
    return calls


class TestMCPConfigShellout:
    """`_configure_mcp` shells out to `claude mcp add/remove` (project scope).

    The original direct-JSON path wrote to `~/.claude/claude_desktop_config.json`
    (which is Claude *Desktop*); Claude Code reads `~/.claude.json` and per-repo
    `<repo>/.mcp.json`. These tests lock in the shellout shape so the bug
    can't regress.
    """

    def test_add_path_argv_and_cwd(self, tmp_path: Path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        calls = _mock_claude_cli(monkeypatch)

        result = _configure_mcp(repo, remove=False)

        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv[:6] == ["claude", "mcp", "add", "--scope", "project", "nauro"]
        assert argv[6] == "--"
        # argv[7] is the resolved nauro binary path
        assert argv[8:] == ["serve", "--stdio"]
        assert kwargs.get("cwd") == repo
        assert kwargs.get("check") is False
        assert "wrote nauro to .mcp.json" in result

    def test_remove_path_argv_and_cwd(self, tmp_path: Path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        # The remove path now pre-checks <repo>/.mcp.json and only invokes
        # `claude mcp remove` when the nauro entry is actually present.
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"nauro": {}}}))
        calls = _mock_claude_cli(monkeypatch)

        result = _configure_mcp(repo, remove=True)

        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv == ["claude", "mcp", "remove", "nauro"]
        assert kwargs.get("cwd") == repo
        assert "removed nauro from .mcp.json" in result

    def test_skips_when_claude_not_on_path(self, tmp_path: Path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        calls = _mock_claude_cli(monkeypatch, on_path=False)

        result = _configure_mcp(repo, remove=False)

        assert calls == []  # subprocess.run never invoked
        assert "skipping Claude Code wiring" in result

    def test_remove_when_claude_not_on_path_says_nothing_to_remove(
        self, tmp_path: Path, monkeypatch
    ):
        """On --remove with no `claude` CLI, surface a remove-shaped message
        rather than telling the user to install Claude Code (which is the
        add-path hint). Locks in the branched message."""
        repo = tmp_path / "repo"
        repo.mkdir()
        calls = _mock_claude_cli(monkeypatch, on_path=False)

        result = _configure_mcp(repo, remove=True)

        assert calls == []
        assert "nothing to remove" in result
        assert "skipping Claude Code wiring" not in result

    def test_non_zero_exit_surfaces_stderr(self, tmp_path: Path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        _mock_claude_cli(monkeypatch, returncode=1, stderr="some claude error")

        result = _configure_mcp(repo, remove=False)

        assert "some claude error" in result
        assert "claude mcp add failed" in result

    def test_remove_skips_when_no_mcp_json(self, tmp_path: Path, monkeypatch):
        """No `.mcp.json` at all → no-op without invoking the CLI."""
        repo = tmp_path / "repo"
        repo.mkdir()
        calls = _mock_claude_cli(monkeypatch)

        result = _configure_mcp(repo, remove=True)

        assert calls == []
        assert "no nauro entry to remove" in result

    def test_remove_skips_when_nauro_absent_from_mcp_json(self, tmp_path: Path, monkeypatch):
        """`.mcp.json` exists but has no nauro entry → no-op without invoking CLI."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {}}}))
        calls = _mock_claude_cli(monkeypatch)

        result = _configure_mcp(repo, remove=True)

        assert calls == []
        assert "no nauro entry to remove" in result

    def test_remove_handles_malformed_mcp_json(self, tmp_path: Path, monkeypatch):
        """Malformed `.mcp.json` surfaces a parse error instead of crashing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".mcp.json").write_text("{not json")
        calls = _mock_claude_cli(monkeypatch)

        result = _configure_mcp(repo, remove=True)

        assert calls == []
        assert "could not parse .mcp.json" in result

    def test_setup_all_iterates_per_repo(self, tmp_path: Path, monkeypatch):
        """Multi-repo project: `setup all` invokes `claude mcp add` once per
        repo with the matching cwd. Locks in the project-scope iteration that
        the multi-repo flow depends on."""
        from nauro.cli.commands.setup import setup_all_surfaces

        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()
        # Pretend HOME exists so claude/codex skill dirs are writable.
        monkeypatch.setenv("HOME", str(tmp_path))
        calls = _mock_claude_cli(monkeypatch)

        setup_all_surfaces([repo1, repo2], remove=False)

        # Two `claude mcp add` calls — one per repo, each with the right cwd.
        add_calls = [(argv, kwargs) for argv, kwargs in calls if argv[2] == "add"]
        assert len(add_calls) == 2
        cwds = {kwargs.get("cwd") for _, kwargs in add_calls}
        assert cwds == {repo1, repo2}


class TestAGENTSMD:
    def test_setup_regenerates_agents_md(self, tmp_path: Path, monkeypatch):
        """Setup regenerates AGENTS.md in all repos."""
        repos = _setup_project(tmp_path, monkeypatch)

        # Mock claude CLI as missing — these tests don't care about wiring,
        # only the surrounding code paths (legacy CLAUDE.md cleanup, AGENTS.md, etc.).
        _mock_claude_cli(monkeypatch, on_path=False)

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

        # Mock claude CLI as missing — these tests don't care about wiring,
        # only the surrounding code paths (legacy CLAUDE.md cleanup, AGENTS.md, etc.).
        _mock_claude_cli(monkeypatch, on_path=False)

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0
        assert not (repos[0] / "CLAUDE.md").exists()

    def test_setup_does_not_modify_existing_claude_md(self, tmp_path: Path, monkeypatch):
        """Setup leaves existing CLAUDE.md untouched (no injection)."""
        repos = _setup_project(tmp_path, monkeypatch)
        existing = "# My Project\n\nSome existing content.\n"
        (repos[0] / "CLAUDE.md").write_text(existing)

        # Mock claude CLI as missing — these tests don't care about wiring,
        # only the surrounding code paths (legacy CLAUDE.md cleanup, AGENTS.md, etc.).
        _mock_claude_cli(monkeypatch, on_path=False)

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

        # Mock claude CLI as missing — these tests don't care about wiring,
        # only the surrounding code paths (legacy CLAUDE.md cleanup, AGENTS.md, etc.).
        _mock_claude_cli(monkeypatch, on_path=False)

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

        # Mock claude CLI as missing — these tests don't care about wiring,
        # only the surrounding code paths (legacy CLAUDE.md cleanup, AGENTS.md, etc.).
        _mock_claude_cli(monkeypatch, on_path=False)

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

        # Mock claude CLI as missing — these tests don't care about wiring,
        # only the surrounding code paths (legacy CLAUDE.md cleanup, AGENTS.md, etc.).
        _mock_claude_cli(monkeypatch, on_path=False)

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

        # Mock claude CLI as missing — these tests don't care about wiring,
        # only the surrounding code paths (legacy CLAUDE.md cleanup, AGENTS.md, etc.).
        _mock_claude_cli(monkeypatch, on_path=False)

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0

        assert (repo1 / "AGENTS.md").exists()
        assert (repo2 / "AGENTS.md").exists()
        # No CLAUDE.md created
        assert not (repo1 / "CLAUDE.md").exists()
        assert not (repo2 / "CLAUDE.md").exists()
