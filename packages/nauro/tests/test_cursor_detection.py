"""Cursor configuration detection and the setup, adopt and status paths it gates."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.integrations import codex_config, cursor, json_mcp
from nauro.cli.integrations.cursor import configure_cursor, cursor_configuration_found
from nauro.cli.main import app
from nauro.setup.outcomes import CursorSkippedOutcome, JsonMcpKind, is_failure
from nauro.setup.render import render
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import git_init, snapshot_tree

runner = CliRunner()

SKIP_LINE = "Cursor configuration not found, skipped; run 'nauro setup cursor' to wire it later."
CURSOR_HANDOFF = (
    "In Cursor Agent chat, type @nauro-adopt to include .cursor/rules/nauro-adopt.mdc. "
    "Commit that file if collaborators should inherit the rule."
)
PRINT_PROMPT_HINT = (
    "Claude Code MCP wiring was not written. To get the adopt instructions anyway, run "
    "'nauro adopt --print-prompt' and paste them into your agent; this does not wire MCP."
)
posix_links = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A created user home that both ``HOME`` and ``USERPROFILE`` name."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


def _dirs(tmp_path: Path, *names: str) -> list[Path]:
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.mkdir()
    return paths


class TestDetection:
    def test_home_cursor_directory_is_found(self, home: Path):
        (home / ".cursor").mkdir()
        assert cursor_configuration_found([]) is True

    def test_nothing_is_not_found(self, home: Path, tmp_path: Path):
        assert cursor_configuration_found(_dirs(tmp_path, "a", "b")) is False

    def test_home_cursor_file_is_not_found(self, home: Path):
        (home / ".cursor").write_text("", encoding="utf-8")
        assert cursor_configuration_found([]) is False

    def test_missing_repo_path_is_not_found(self, home: Path, tmp_path: Path):
        assert cursor_configuration_found([tmp_path / "gone"]) is False

    def test_one_detected_repo_among_several_is_found(self, home: Path, tmp_path: Path):
        repos = _dirs(tmp_path, "a", "b", "c")
        (repos[1] / ".cursor").mkdir()
        assert cursor_configuration_found(repos) is True

    def test_repo_cursor_file_is_not_found(self, home: Path, tmp_path: Path):
        [repo] = _dirs(tmp_path, "repo")
        (repo / ".cursor").write_text("", encoding="utf-8")
        assert cursor_configuration_found([repo]) is False

    @posix_links
    def test_home_cursor_symlink_to_directory_is_found(self, home: Path, tmp_path: Path):
        [target] = _dirs(tmp_path, "elsewhere")
        (home / ".cursor").symlink_to(target)
        assert cursor_configuration_found([]) is True

    @posix_links
    def test_dangling_home_cursor_symlink_is_not_found(self, home: Path, tmp_path: Path):
        (home / ".cursor").symlink_to(tmp_path / "gone")
        assert cursor_configuration_found([]) is False

    @posix_links
    def test_repo_cursor_symlink_is_found_without_reading_through(self, home: Path, tmp_path: Path):
        [repo] = _dirs(tmp_path, "repo")
        (repo / ".cursor").symlink_to(tmp_path / "gone")
        assert cursor_configuration_found([repo]) is True

    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as non-root"
    )
    def test_uninspectable_home_counts_as_found(self, home: Path):
        home.chmod(0)
        try:
            assert cursor_configuration_found([]) is True
        finally:
            home.chmod(0o755)

    @pytest.mark.parametrize(
        "code, found",
        [
            (errno.ENOENT, False),
            (errno.ENOTDIR, False),
            (errno.EACCES, True),
            (errno.EIO, True),
            (errno.ELOOP, True),
        ],
    )
    @pytest.mark.parametrize("probe", ["home", "repo"])
    def test_inspection_errors(
        self, home: Path, tmp_path: Path, monkeypatch, probe: str, code: int, found: bool
    ):
        [repo] = _dirs(tmp_path, "repo")
        target = home / ".cursor" if probe == "home" else repo / ".cursor"
        real_stat = Path.stat

        def stat(self, *, follow_symlinks=True):
            if self == target and follow_symlinks is (probe == "home"):
                raise OSError(code, os.strerror(code), str(self))
            return real_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", stat)
        monkeypatch.setattr(Path, "lstat", lambda self: stat(self, follow_symlinks=False))

        assert cursor_configuration_found([repo]) is found

    def test_detection_and_global_wiring_resolve_the_same_home(self, home: Path):
        (home / ".cursor").mkdir()

        outcome = configure_cursor(remove=False)

        assert cursor_configuration_found([]) is True
        assert outcome.kind is JsonMcpKind.WROTE
        assert outcome.label == str(Path.home() / ".cursor" / "mcp.json")
        assert (home / ".cursor" / "mcp.json").is_file()


def test_skipped_outcome_renders_the_skip_line_and_is_not_a_failure():
    assert render(CursorSkippedOutcome()) == [SKIP_LINE]
    assert not is_failure(CursorSkippedOutcome())


# ─── setup all ────────────────────────────────────────────────────────────────


def _register(tmp_path: Path, monkeypatch, *names: str) -> list[Path]:
    repos = _dirs(tmp_path, *names)
    _pid, store = register_project_v2("proj", repos)
    scaffold_project_store("proj", store)
    monkeypatch.chdir(repos[0])
    return repos


def _cursor_paths(root: Path) -> list[str]:
    return [path for path in snapshot_tree(root) if ".cursor" in path]


class TestSetupAll:
    def test_not_found_skips_cursor_and_writes_no_cursor_files(
        self, home: Path, tmp_path: Path, monkeypatch
    ):
        _register(tmp_path, monkeypatch, "repo")

        result = runner.invoke(app, ["setup", "all", "--with-skills", "--with-subagents"])

        assert result.exit_code == 0, result.output
        assert SKIP_LINE in result.stdout.splitlines()
        assert _cursor_paths(tmp_path) == []
        assert not (home / ".cursor").exists()
        assert (home / ".codex" / "config.toml").is_file()
        assert (tmp_path / "repo" / ".mcp.json").is_file()

    def test_prior_nauro_rules_count_as_found(self, home: Path, tmp_path: Path, monkeypatch):
        [repo] = _register(tmp_path, monkeypatch, "repo")
        (repo / ".cursor" / "rules").mkdir(parents=True)
        (repo / ".cursor" / "rules" / "nauro-adopt.mdc").write_text("old\n", encoding="utf-8")

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0, result.output
        assert SKIP_LINE not in result.stdout
        assert (home / ".cursor" / "mcp.json").is_file()

    def test_explicit_setup_cursor_then_setup_all_adds_rules_and_agents(
        self, home: Path, tmp_path: Path, monkeypatch
    ):
        [repo] = _register(tmp_path, monkeypatch, "repo")

        assert runner.invoke(app, ["setup", "cursor"]).exit_code == 0
        assert (home / ".cursor" / "mcp.json").is_file()
        result = runner.invoke(app, ["setup", "all", "--with-subagents"])

        assert result.exit_code == 0, result.output
        assert SKIP_LINE not in result.stdout
        assert (repo / ".cursor" / "rules" / "nauro-adopt.mdc").is_file()
        assert (repo / ".cursor" / "agents" / "nauro-planner.md").is_file()

    def test_legacy_repo_wiring_migrates_to_global(self, home: Path, tmp_path: Path, monkeypatch):
        [repo] = _register(tmp_path, monkeypatch, "repo")
        command = json_mcp._find_nauro_command()
        legacy = repo / ".cursor" / "mcp.json"
        legacy.parent.mkdir()
        entry = {"type": "stdio", "command": command, "args": ["serve", "--stdio"]}
        legacy.write_text(json.dumps({"mcpServers": {"nauro": entry}}), encoding="utf-8")

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        wrote = lines.index(f"  {home}: wrote nauro to {home / '.cursor' / 'mcp.json'}")
        assert lines[wrote + 1] == f"  {repo}: removed nauro from .cursor/mcp.json"
        assert not legacy.exists()

    def test_removal_after_not_found_creates_no_cursor_paths(
        self, home: Path, tmp_path: Path, monkeypatch
    ):
        _register(tmp_path, monkeypatch, "repo")
        assert runner.invoke(app, ["setup", "all"]).exit_code == 0

        result = runner.invoke(app, ["setup", "all", "--remove"])

        assert result.exit_code == 0, result.output
        assert _cursor_paths(tmp_path) == []
        assert not (home / ".cursor").exists()
        assert not (tmp_path / "repo" / ".cursor").exists()


# ─── adopt ───────────────────────────────────────────────────────────────────


@pytest.fixture
def git_repo(home: Path, tmp_path: Path, monkeypatch) -> Path:
    [repo] = _dirs(tmp_path, "myrepo")
    git_init(repo)
    monkeypatch.chdir(repo)
    return repo


def _deny_repo_mcp_writes(monkeypatch) -> None:
    def _denied(path, _data):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(path))

    monkeypatch.setattr(json_mcp, "write_json_config", _denied)


class TestAdopt:
    def test_not_found_skips_cursor_and_omits_the_cursor_handoff(
        self, home: Path, git_repo: Path, tmp_path: Path
    ):
        result = runner.invoke(app, ["adopt", "--name", "proj"])

        assert result.exit_code == 0, result.output
        assert SKIP_LINE in result.stdout.splitlines()
        assert CURSOR_HANDOFF not in result.stdout
        assert result.stdout.rstrip().endswith(
            "Use /nauro-adopt in Claude Code or $nauro-adopt in Codex."
        )
        assert _cursor_paths(tmp_path) == []

    def test_found_wires_cursor_and_names_the_rule(self, home: Path, git_repo: Path):
        (home / ".cursor").mkdir()

        result = runner.invoke(app, ["adopt", "--name", "proj"])

        assert result.exit_code == 0, result.output
        assert SKIP_LINE not in result.stdout
        assert result.stdout.rstrip().endswith(f"$nauro-adopt in Codex. {CURSOR_HANDOFF}")

    def test_no_setup_omits_the_cursor_handoff(self, home: Path, git_repo: Path):
        (home / ".cursor").mkdir()

        result = runner.invoke(app, ["adopt", "--name", "proj", "--no-setup-and-skills"])

        assert result.exit_code == 0, result.output
        assert CURSOR_HANDOFF not in result.stdout

    def test_fresh_mcp_failure_names_print_prompt(self, git_repo: Path, monkeypatch):
        _deny_repo_mcp_writes(monkeypatch)

        result = runner.invoke(app, ["adopt", "--name", "proj"])

        assert result.exit_code == 1, result.output
        assert PRINT_PROMPT_HINT in result.stderr
        assert result.stderr.index(PRINT_PROMPT_HINT) < result.stderr.index(
            "re-run 'nauro setup all'"
        )

    def test_repeat_mcp_failure_names_print_prompt(self, git_repo: Path, monkeypatch):
        assert runner.invoke(app, ["adopt", "--name", "proj"]).exit_code == 0
        _deny_repo_mcp_writes(monkeypatch)

        result = runner.invoke(app, ["adopt", "--with-skills"])

        assert result.exit_code == 1, result.output
        assert PRINT_PROMPT_HINT in result.stderr

    def test_non_mcp_failure_omits_print_prompt(self, home: Path, git_repo: Path, monkeypatch):
        monkeypatch.setattr(
            codex_config, "codex_config_path", lambda: home / "codex-dir" / "config.toml"
        )
        (home / "codex-dir" / "config.toml").mkdir(parents=True)

        result = runner.invoke(app, ["adopt", "--name", "proj"])

        assert result.exit_code == 1, result.output
        assert PRINT_PROMPT_HINT not in result.output

    def test_cursor_mcp_failure_omits_print_prompt(self, home: Path, git_repo: Path, monkeypatch):
        (home / ".cursor").mkdir()

        def _denied(path, _data):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))

        monkeypatch.setattr(cursor, "write_json_config", _denied)

        result = runner.invoke(app, ["adopt", "--name", "proj"])

        assert result.exit_code == 1, result.output
        assert f"could not write {home / '.cursor' / 'mcp.json'}" in result.stdout
        assert (git_repo / ".mcp.json").is_file()
        assert PRINT_PROMPT_HINT not in result.output

    def test_unadopt_failure_omits_print_prompt(self, git_repo: Path):
        assert runner.invoke(app, ["adopt", "--name", "proj"]).exit_code == 0
        (git_repo / ".mcp.json").unlink()
        (git_repo / ".mcp.json").mkdir()

        result = runner.invoke(app, ["adopt", "--remove", "--yes"])

        assert result.exit_code == 1, result.output
        assert PRINT_PROMPT_HINT not in result.output


# ─── status ──────────────────────────────────────────────────────────────────


class TestStatus:
    def test_claude_and_codex_only_is_active_without_a_cursor_segment(
        self, home: Path, tmp_path: Path, monkeypatch
    ):
        _register(tmp_path, monkeypatch, "repo")
        args = ["setup", "all", "--with-skills", "--with-subagents"]
        assert runner.invoke(app, args).exit_code == 0

        human = runner.invoke(app, ["status", "--no-probe"])
        payload = json.loads(runner.invoke(app, ["status", "--json", "--no-probe"]).stdout)

        assert human.exit_code == 0, human.output
        assert "  Skills        active (Claude 5/5; Codex 5/5)" in human.stdout
        assert "  Workflow      active (Claude 5/5; Codex 5/5)" in human.stdout
        assert payload["skills"]["core"]["cursor"] == {"present": 0, "current": 0, "expected": 0}
        assert payload["workflow_agents"]["cursor"] == {
            "present": 0,
            "current": 0,
            "expected": 0,
        }
