"""Tests for the ``--format`` flag on the auto-generated tool-mirror commands.

The contract under test:

- ``--format json`` (the default) is byte-identical to the historical
  JSON-only surface, pinned across all ten commands with static mocked
  adapters (write envelopes embed minted ids/timestamps, so a real store
  cannot give byte identity).
- ``--format text`` renders the envelope through
  ``nauro_core.renderers.RENDERERS`` with the same per-tool kwargs the
  stdio server threads, and the rendered stdout is the sole carrier of
  error/guidance prose (no stderr duplicate).
- Write tools and raised renderers fall back to the JSON envelope with
  json-mode streams. Exit codes are format-independent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nauro_core.renderers import RENDERERS
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.mcp import tools as mcp_tools
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config

runner = CliRunner()

# Minimal happy-path argv per auto-gen command.
COMMAND_ARGS: dict[str, list[str]] = {
    "get_context": [],
    "get_raw_file": ["project.md"],
    "list_decisions": [],
    "get_decision": ["1"],
    "diff_since_last_session": [],
    "search_decisions": ["budget"],
    "check_decision": ["Store dollar amounts as decimal numbers"],
    "update_state": ["Shipped the format-flag matrix"],
    "flag_question": ["Should the cache invalidate on write?"],
    "propose_decision": ["A rationale long enough for the format-matrix run."],
}

WRITE_TOOLS = frozenset({"update_state", "flag_question", "propose_decision"})
READ_TOOLS = frozenset(COMMAND_ARGS) - WRITE_TOOLS


def _argv(tool_name: str) -> list[str]:
    return [tool_name.replace("_", "-"), *COMMAND_ARGS[tool_name]]


@pytest.fixture
def demo_repo(tmp_path: Path, monkeypatch) -> Path:
    """Local-mode demo project rooted at tmp_path/repo with cwd inside it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("demo-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "demo-project"})
    create_demo_project(store_path)
    monkeypatch.chdir(repo)
    return store_path


class TestJsonModeByteIdentity:
    @pytest.mark.parametrize("tool_name", sorted(COMMAND_ARGS))
    def test_default_output_matches_format_json(self, tool_name, demo_repo, monkeypatch) -> None:
        canned = {"store": "local", "status": "ok", "tool": tool_name}
        monkeypatch.setattr(
            mcp_tools, f"tool_{tool_name}", lambda store_path, *a, **k: dict(canned)
        )
        default = runner.invoke(app, _argv(tool_name))
        explicit = runner.invoke(app, [*_argv(tool_name), "--format", "json"])
        assert default.exit_code == 0, default.output
        assert explicit.exit_code == 0, explicit.output
        expected = json.dumps(canned, indent=2) + "\n"
        assert default.stdout == expected
        assert explicit.stdout == expected


class TestTextMode:
    @pytest.mark.parametrize("tool_name", sorted(READ_TOOLS))
    def test_text_mode_equals_renderer_output(self, tool_name, demo_repo) -> None:
        json_run = runner.invoke(app, [*_argv(tool_name), "--format", "json"])
        assert json_run.exit_code == 0, json_run.output
        envelope = json.loads(json_run.stdout)
        renderer_kwargs = {}
        if tool_name == "get_decision":
            renderer_kwargs = {"mode": "full"}
        if tool_name == "search_decisions":
            renderer_kwargs = {"query": COMMAND_ARGS[tool_name][0]}
        expected = RENDERERS[tool_name](envelope, **renderer_kwargs)
        text_run = runner.invoke(app, [*_argv(tool_name), "--format", "text"])
        assert text_run.exit_code == 0, text_run.output
        assert text_run.stdout == expected + "\n"

    def test_get_decision_mode_threads_to_renderer(self, demo_repo) -> None:
        argv = ["get-decision", "1", "--mode", "header"]
        envelope = json.loads(runner.invoke(app, [*argv, "--format", "json"]).stdout)
        expected = RENDERERS["get_decision"](envelope, mode="header")
        result = runner.invoke(app, [*argv, "--format", "text"])
        assert result.exit_code == 0, result.output
        assert result.stdout == expected + "\n"

    def test_search_decisions_query_threads_to_renderer(self, demo_repo) -> None:
        result = runner.invoke(app, ["search-decisions", "budget", "--format", "text"])
        assert result.exit_code == 0, result.output
        # The rendered header echoes the query only when it is threaded
        # through renderer kwargs; the kernel envelope omits it.
        assert 'for "budget"' in result.stdout

    def test_error_envelope_renders_without_stderr_duplicate(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-decision", "9999", "--format", "text"])
        assert result.exit_code == 1
        assert result.stdout.startswith("Error:")
        assert result.stderr == ""

    def test_error_envelope_json_mode_keeps_stderr_guidance(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-decision", "9999", "--format", "json"])
        assert result.exit_code == 1
        assert json.loads(result.stdout).get("error")
        assert result.stderr.strip()


class TestJsonFallback:
    def test_write_tool_text_mode_falls_back_to_json_streams(self, demo_repo, monkeypatch) -> None:
        canned = {"store": "local", "status": "ok"}
        monkeypatch.setattr(
            mcp_tools, "tool_update_state", lambda store_path, *a, **k: dict(canned)
        )
        text = runner.invoke(app, ["update-state", "delta text", "--format", "text"])
        json_mode = runner.invoke(app, ["update-state", "delta text", "--format", "json"])
        assert text.exit_code == 0
        assert text.stdout == json_mode.stdout
        assert json.loads(text.stdout) == canned

    def test_rejected_envelope_exits_zero_with_json_fallback(self, demo_repo, monkeypatch) -> None:
        rejected = {
            "store": "local",
            "status": "rejected",
            "error": {"kind": "rejected", "reason": "Rationale too short."},
        }
        monkeypatch.setattr(
            mcp_tools, "tool_propose_decision", lambda store_path, **k: dict(rejected)
        )
        result = runner.invoke(app, ["propose-decision", "some rationale", "--format", "text"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["status"] == "rejected"
        assert result.stderr == ""

    def test_raised_renderer_falls_back_to_json(self, demo_repo, monkeypatch) -> None:
        def explode(envelope, **kwargs):
            raise RuntimeError("renderer kaboom")

        monkeypatch.setitem(RENDERERS, "list_decisions", explode)
        result = runner.invoke(app, ["list-decisions", "--format", "text"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert "decisions" in envelope
        # The fallback is silent: json-mode streams only, no traceback or
        # log line on stderr.
        assert result.stderr == ""
