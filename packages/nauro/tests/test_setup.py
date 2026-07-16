"""Tests for nauro setup claude-code command."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.integrations.codex_config import _configure_codex
from nauro.cli.integrations.json_mcp import _configure_mcp, recorded_mcp_commands
from nauro.cli.integrations.legacy import CLAUDE_MD_END, CLAUDE_MD_START
from nauro.cli.integrations.outcomes import CodexConfigKind, JsonMcpKind
from nauro.cli.main import app
from nauro.store.registry import register_project, register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _setup_project(tmp_path: Path, monkeypatch, repo_paths: list[Path] | None = None):
    """Helper to create a project with repos."""
    if repo_paths is None:
        repo_paths = [tmp_path / "repo"]
        repo_paths[0].mkdir()
    store = register_project("testproj", repo_paths)
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(repo_paths[0])
    return repo_paths


class TestMCPConfigDirectWrite:
    """`_configure_mcp` writes ``<repo>/.mcp.json`` directly.

    The format is the documented Claude Code project-scope shape: an
    ``mcpServers`` object map keyed by server name. These tests lock in the
    file contents and merge behavior so the direct-write contract does not
    silently regress.
    """

    def test_add_writes_mcp_json(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()

        result = _configure_mcp(repo, remove=False)

        config = json.loads((repo / ".mcp.json").read_text())
        assert "nauro" in config["mcpServers"]
        entry = config["mcpServers"]["nauro"]
        assert entry["args"] == ["serve", "--stdio"]
        assert isinstance(entry["command"], str) and entry["command"]
        assert result.kind is JsonMcpKind.WROTE

    def test_add_preserves_existing_servers(self, tmp_path: Path):
        """Existing servers in `.mcp.json` are preserved when we add nauro."""
        repo = tmp_path / "repo"
        repo.mkdir()
        existing = {"mcpServers": {"other": {"command": "/usr/local/bin/other", "args": ["serve"]}}}
        (repo / ".mcp.json").write_text(json.dumps(existing))

        result = _configure_mcp(repo, remove=False)

        config = json.loads((repo / ".mcp.json").read_text())
        assert config["mcpServers"]["other"] == existing["mcpServers"]["other"]
        assert "nauro" in config["mcpServers"]
        assert result.kind is JsonMcpKind.WROTE

    def test_add_overwrites_stale_nauro_entry(self, tmp_path: Path):
        """A pre-existing `nauro` entry is overwritten with the current command path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        stale = {"mcpServers": {"nauro": {"command": "/old/path/to/nauro", "args": ["different"]}}}
        (repo / ".mcp.json").write_text(json.dumps(stale))

        _configure_mcp(repo, remove=False)

        config = json.loads((repo / ".mcp.json").read_text())
        assert config["mcpServers"]["nauro"]["args"] == ["serve", "--stdio"]
        assert config["mcpServers"]["nauro"]["command"] != "/old/path/to/nauro"

    def test_add_surfaces_parse_error_without_clobbering(self, tmp_path: Path):
        """Malformed `.mcp.json` on the add path surfaces a parse error and
        leaves the existing file untouched."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".mcp.json").write_text("{not json")

        result = _configure_mcp(repo, remove=False)

        assert result.kind is JsonMcpKind.PARSE_ERROR
        assert (repo / ".mcp.json").read_text() == "{not json"

    def test_add_surfaces_invalid_utf8_without_clobbering(self, tmp_path: Path):
        """A non-UTF-8 `.mcp.json` surfaces the parse-error status instead of
        raising, and the existing bytes are left untouched."""
        repo = tmp_path / "repo"
        repo.mkdir()
        raw = b'\xff\xfe{"mcpServers": {}}'
        (repo / ".mcp.json").write_bytes(raw)

        result = _configure_mcp(repo, remove=False)

        assert result.kind is JsonMcpKind.PARSE_ERROR
        assert (repo / ".mcp.json").read_bytes() == raw

    def test_add_round_trips_non_ascii_content_as_utf8(self, tmp_path: Path):
        """Existing non-ASCII server config survives the add as UTF-8 bytes,
        independent of the platform's locale encoding."""
        repo = tmp_path / "repo"
        repo.mkdir()
        seeded = json.dumps(
            {"mcpServers": {"other": {"command": "café", "args": []}}},
            ensure_ascii=False,
        )
        (repo / ".mcp.json").write_bytes(seeded.encode("utf-8"))

        result = _configure_mcp(repo, remove=False)

        assert result.kind is JsonMcpKind.WROTE
        data = json.loads((repo / ".mcp.json").read_bytes().decode("utf-8"))
        assert data["mcpServers"]["other"]["command"] == "café"
        assert "nauro" in data["mcpServers"]

    def test_remove_deletes_nauro_entry_and_unlinks_empty_file(self, tmp_path: Path):
        """Remove the nauro entry; if mcpServers becomes empty, drop the file."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"nauro": {"command": "/x", "args": []}}})
        )

        result = _configure_mcp(repo, remove=True)

        assert result.kind is JsonMcpKind.REMOVED
        assert not (repo / ".mcp.json").exists()

    def test_remove_preserves_other_servers(self, tmp_path: Path):
        """Removing nauro keeps other server entries and rewrites the file."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "nauro": {"command": "/x", "args": []},
                        "other": {"command": "/y", "args": []},
                    }
                }
            )
        )

        result = _configure_mcp(repo, remove=True)

        assert result.kind is JsonMcpKind.REMOVED
        config = json.loads((repo / ".mcp.json").read_text())
        assert "nauro" not in config["mcpServers"]
        assert "other" in config["mcpServers"]

    def test_remove_skips_when_no_mcp_json(self, tmp_path: Path):
        """No `.mcp.json` at all → no-op."""
        repo = tmp_path / "repo"
        repo.mkdir()

        result = _configure_mcp(repo, remove=True)

        assert result.kind is JsonMcpKind.NOTHING_TO_REMOVE
        assert not (repo / ".mcp.json").exists()

    def test_remove_skips_when_nauro_absent_from_mcp_json(self, tmp_path: Path):
        """`.mcp.json` exists but has no nauro entry → no-op, file untouched."""
        repo = tmp_path / "repo"
        repo.mkdir()
        original = json.dumps({"mcpServers": {"other": {}}})
        (repo / ".mcp.json").write_text(original)

        result = _configure_mcp(repo, remove=True)

        assert result.kind is JsonMcpKind.NOTHING_TO_REMOVE
        assert (repo / ".mcp.json").read_text() == original

    def test_remove_handles_malformed_mcp_json(self, tmp_path: Path):
        """Malformed `.mcp.json` surfaces a parse error instead of crashing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".mcp.json").write_text("{not json")

        result = _configure_mcp(repo, remove=True)

        assert result.kind is JsonMcpKind.PARSE_ERROR

    def test_setup_all_iterates_per_repo(self, tmp_path: Path, monkeypatch):
        """Multi-repo project: `setup all` writes `.mcp.json` once per repo."""
        from nauro.cli.integrations.orchestrator import setup_all_surfaces

        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()
        # HOME redirect so claude/codex skill dirs land under tmp_path.
        monkeypatch.setenv("HOME", str(tmp_path))

        setup_all_surfaces([repo1, repo2], remove=False)

        for repo in (repo1, repo2):
            config = json.loads((repo / ".mcp.json").read_text())
            assert "nauro" in config["mcpServers"]


class TestAGENTSMD:
    def test_setup_regenerates_agents_md(self, tmp_path: Path, monkeypatch):
        """Setup regenerates AGENTS.md in all repos."""
        repos = _setup_project(tmp_path, monkeypatch)

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

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0
        assert not (repos[0] / "CLAUDE.md").exists()

    def test_setup_does_not_modify_existing_claude_md(self, tmp_path: Path, monkeypatch):
        """Setup leaves existing CLAUDE.md untouched (no injection)."""
        repos = _setup_project(tmp_path, monkeypatch)
        existing = "# My Project\n\nSome existing content.\n"
        (repos[0] / "CLAUDE.md").write_text(existing)

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

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0
        assert not (repos[0] / "CLAUDE.md").exists()


class TestProjectResolution:
    def test_project_flag_overrides_cwd(self, tmp_path: Path, monkeypatch):
        """--project flag overrides cwd resolution."""

        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        store_a = register_project("proj-a", [repo_a])
        scaffold_project_store("proj-a", store_a)
        store_b = register_project("proj-b", [repo_b])
        scaffold_project_store("proj-b", store_b)

        monkeypatch.chdir(repo_a)

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

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])
        assert result.exit_code == 0

        assert (repo1 / "AGENTS.md").exists()
        assert (repo2 / "AGENTS.md").exists()
        # No CLAUDE.md created
        assert not (repo1 / "CLAUDE.md").exists()
        assert not (repo2 / "CLAUDE.md").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
class TestSymlinkRefusal:
    """Repo-scoped setup writers refuse pre-planted symlinks in the checkout."""

    def test_setup_claude_code_refuses_symlinked_mcp_json(self, tmp_path: Path, monkeypatch):
        repos = _setup_project(tmp_path, monkeypatch)
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        (repos[0] / ".mcp.json").symlink_to(outside)

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])

        assert result.exit_code == 0
        assert "refused to modify" in result.output
        assert "it is a symlink" in result.output
        # Never written through, never replaced.
        assert (repos[0] / ".mcp.json").is_symlink()
        assert outside.read_text() == "{}"

    def test_legacy_cleanup_refuses_symlinked_claude_md(self, tmp_path: Path, monkeypatch):
        repos = _setup_project(tmp_path, monkeypatch)
        content = f"{CLAUDE_MD_START}\nold block\n{CLAUDE_MD_END}\n"
        outside = tmp_path / "outside.md"
        outside.write_text(content)
        (repos[0] / "CLAUDE.md").symlink_to(outside)

        result = runner.invoke(app, ["setup", "claude-code", "--project", "testproj"])

        assert result.exit_code == 0
        assert "refused to modify" in result.output
        assert (repos[0] / "CLAUDE.md").is_symlink()
        assert outside.read_text() == content

    def test_setup_all_declines_symlinked_repo_config(self, tmp_path: Path, monkeypatch):
        """A planted config symlink must not select the project whose surfaces
        get wired: cwd resolution declines the link, and setup falls through
        to the no-project error without writing anything."""
        monkeypatch.setenv("HOME", str(tmp_path))
        victim = tmp_path / "victim"
        victim.mkdir()
        pid, _store = register_project_v2("victim", [victim])
        save_repo_config(victim, {"mode": "local", "id": pid, "name": "victim"})

        attack = tmp_path / "attack"
        (attack / ".nauro").mkdir(parents=True)
        (attack / ".nauro" / "config.json").symlink_to(victim / ".nauro" / "config.json")
        monkeypatch.chdir(attack)

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 1
        assert "No project found" in result.output
        assert not (attack / ".mcp.json").exists()
        assert not (attack / ".cursor").exists()
        assert not (attack / "AGENTS.md").exists()


class TestMalformedConfigGuards:
    """A pre-existing, off-shape MCP config must skip cleanly, not crash."""

    def test_json_top_level_array_is_skipped(self, tmp_path: Path):
        (tmp_path / ".mcp.json").write_text("[]")
        line = _configure_mcp(tmp_path, remove=False)
        assert line.kind is JsonMcpKind.NOT_JSON_OBJECT
        # Left untouched, not crashed or clobbered.
        assert (tmp_path / ".mcp.json").read_text().strip() == "[]"

    def test_mcpservers_non_object_is_skipped(self, tmp_path: Path):
        original = '{"mcpServers": "oops"}'
        (tmp_path / ".mcp.json").write_text(original)
        line = _configure_mcp(tmp_path, remove=False)
        assert line.kind is JsonMcpKind.MCPSERVERS_NOT_OBJECT
        # The off-shape add path must not clobber the file.
        assert (tmp_path / ".mcp.json").read_text() == original

    def test_mcpservers_non_object_remove_is_noop(self, tmp_path: Path):
        (tmp_path / ".mcp.json").write_text('{"mcpServers": "oops"}')
        line = _configure_mcp(tmp_path, remove=True)
        assert line.kind is JsonMcpKind.NOTHING_TO_REMOVE

    def test_mcpservers_null_add_is_skipped(self, tmp_path: Path):
        """An explicit JSON null mcpServers is a graceful shape-skip, not a crash."""
        original = '{"mcpServers": null}'
        (tmp_path / ".mcp.json").write_text(original)
        line = _configure_mcp(tmp_path, remove=False)
        assert line.kind is JsonMcpKind.MCPSERVERS_NOT_OBJECT
        # The present-but-null container must not be mutated or clobbered.
        assert (tmp_path / ".mcp.json").read_text() == original

    def test_mcpservers_null_remove_is_noop(self, tmp_path: Path):
        (tmp_path / ".mcp.json").write_text('{"mcpServers": null}')
        line = _configure_mcp(tmp_path, remove=True)
        assert line.kind is JsonMcpKind.NOTHING_TO_REMOVE

    def test_recorded_mcp_commands_null_mcpservers_is_empty(self, tmp_path: Path):
        """The status inspector treats a null mcpServers as unwired, never crashes."""
        (tmp_path / ".mcp.json").write_text('{"mcpServers": null}')
        assert recorded_mcp_commands(tmp_path) == []

    def test_codex_non_table_mcp_servers_is_skipped(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('mcp_servers = "oops"\n')
        line = _configure_codex(remove=False, config_path=cfg)
        assert line.kind is CodexConfigKind.MCPSERVERS_NOT_TABLE
        # Original content preserved.
        assert 'mcp_servers = "oops"' in cfg.read_text()

    def test_codex_non_table_mcp_servers_remove_is_noop(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('mcp_servers = "oops"\n')
        line = _configure_codex(remove=True, config_path=cfg)
        assert line.kind is CodexConfigKind.NOTHING_TO_REMOVE
        assert 'mcp_servers = "oops"' in cfg.read_text()
