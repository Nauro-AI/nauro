"""Local-surface wiring for bounded rendered reads.

Pins the two consumers of the shared renderer-kwarg resolver:

* The stdio MCP server returns the context guard report as the sole
  ``content[0]`` block for an oversized L2 payload, and threads the
  canonical file path so alias spellings reach the right bounded-read rule.
* The CLI ``--format text`` mode is capped through the same resolver while
  the default ``--format json`` envelope stays byte-complete - the local
  recovery channel.

Canonicalization itself (``canonical_store_relative_path``) is pinned here
too, including the ``context/../open-questions.md`` alias.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp.rendering import resolve_renderer_kwargs, try_render_envelope
from nauro.mcp.stdio_server import get_context, get_raw_file
from nauro.store.filesystem_store import canonical_store_relative_path
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

# An open-questions file over the 40,000-char render budget whose resolved
# section only elides when the canonical path routes it to the hot-file rule
# (generic head-keep would retain the preamble and cut inside the resolved
# body instead of eliding it behind the section marker).
_OQ_PREAMBLE = "# Open Questions\n" + ("p" * 99 + "\n") * 200
_OQ_RESOLVED = "## Resolved\n" + ("r" * 99 + "\n") * 300
_OQ_CONTENT = _OQ_PREAMBLE + _OQ_RESOLVED


@pytest.fixture
def bounded_repo(tmp_path: Path, monkeypatch) -> Path:
    """Registered local-mode project with cwd inside its repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("bounded", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "bounded"})
    scaffold_project_store("bounded", store_path)
    monkeypatch.chdir(repo)
    return store_path


class TestCanonicalStoreRelativePath:
    def test_parent_alias_resolves_to_hot_file(self, tmp_path: Path):
        assert (
            canonical_store_relative_path(tmp_path, "context/../open-questions.md")
            == "open-questions.md"
        )

    def test_plain_paths_pass_through(self, tmp_path: Path):
        assert canonical_store_relative_path(tmp_path, "state_history.md") == "state_history.md"
        assert canonical_store_relative_path(tmp_path, "context/brief.md") == "context/brief.md"

    def test_escapes_yield_none(self, tmp_path: Path):
        assert canonical_store_relative_path(tmp_path, "../outside.md") is None
        assert canonical_store_relative_path(tmp_path, "/etc/passwd") is None


class TestRendererKwargResolver:
    def test_get_raw_file_threads_canonical_path(self, tmp_path: Path):
        kwargs = resolve_renderer_kwargs(
            "get_raw_file", {"path": "context/../open-questions.md"}, tmp_path
        )
        assert kwargs == {"path": "open-questions.md"}

    def test_get_raw_file_escape_omits_kwarg(self, tmp_path: Path):
        assert resolve_renderer_kwargs("get_raw_file", {"path": "../x.md"}, tmp_path) == {}

    def test_get_raw_file_without_store_omits_kwarg(self):
        assert resolve_renderer_kwargs("get_raw_file", {"path": "stack.md"}, None) == {}

    def test_get_context_threads_level(self, tmp_path: Path):
        assert resolve_renderer_kwargs("get_context", {"level": "L2"}, tmp_path) == {"level": "L2"}

    def test_mode_and_query_thread_as_before(self, tmp_path: Path):
        assert resolve_renderer_kwargs("get_decision", {"mode": "header"}, tmp_path) == {
            "mode": "header"
        }
        assert resolve_renderer_kwargs("search_decisions", {"query": "q"}, tmp_path) == {
            "query": "q"
        }

    def test_unknown_tool_and_absent_args_yield_empty(self, tmp_path: Path):
        assert resolve_renderer_kwargs("list_decisions", {}, tmp_path) == {}
        assert resolve_renderer_kwargs("get_context", {}, tmp_path) == {}


class TestNoJsonFallbackOnMalformedMetadata:
    """A malformed envelope field must never raise out of the renderer.

    Both local surfaces fall back to dumping the complete JSON envelope when
    a renderer raises; with an oversized body alongside malformed metadata
    that fallback would bypass the bounded render entirely.
    """

    def test_malformed_guidance_with_oversized_content_stays_bounded(self):
        outcome = try_render_envelope(
            "get_raw_file", {"guidance": 42, "content": "a" * 50_000}, {"path": "stack.md"}
        )
        assert outcome.failure is None
        assert outcome.text is not None
        assert len(outcome.text) < 41_000

    def test_malformed_available_files_stays_rendered(self):
        envelope = {
            "error": {"kind": "error", "reason": "File not found: nope.md"},
            "available_files": 7,
            "content": "a" * 100_000,
        }
        outcome = try_render_envelope("get_raw_file", envelope, {})
        assert outcome.failure is None
        assert outcome.text == "Error: File not found: nope.md"


class TestStdioSurface:
    def test_oversized_l2_returns_guard_report_as_sole_block(self, bounded_repo: Path):
        (bounded_repo / "state_history.md").write_text(("h" * 99 + "\n") * 2_500)
        result = get_context(project_id="bounded", level=2)
        assert isinstance(result, CallToolResult)
        assert len(result.content) == 1
        text = result.content[0].text
        assert "Context withheld" in text
        assert "(level L2)" in text
        assert len(text) < 2_000
        assert "h" * 99 not in text

    def test_l0_under_budget_not_gated(self, bounded_repo: Path):
        result = get_context(project_id="bounded", level=0)
        assert "Context withheld" not in result.content[0].text

    def test_raw_file_alias_routes_to_open_questions_rule(self, bounded_repo: Path):
        (bounded_repo / "open-questions.md").write_text(_OQ_CONTENT)
        result = get_raw_file(path="context/../open-questions.md", project_id="bounded")
        text = result.content[0].text
        assert "Resolved section elided" in text
        assert "r" * 99 not in text
        assert "p" * 99 in text


class TestCliSurface:
    def test_text_capped_while_default_json_byte_complete(self, bounded_repo: Path):
        content = ("h" * 99 + "\n") * 600  # 60,000 chars
        (bounded_repo / "state_history.md").write_text(content)

        json_run = runner.invoke(app, ["get-raw-file", "state_history.md", "--format", "json"])
        assert json_run.exit_code == 0, json_run.output
        assert json.loads(json_run.stdout)["content"] == content

        text_run = runner.invoke(app, ["get-raw-file", "state_history.md", "--format", "text"])
        assert text_run.exit_code == 0, text_run.output
        assert "chars omitted from the start of state_history.md" in text_run.stdout
        assert len(text_run.stdout) < 41_000

    def test_alias_reaches_hot_file_rule_in_text_mode(self, bounded_repo: Path):
        (bounded_repo / "open-questions.md").write_text(_OQ_CONTENT)
        argv = ["get-raw-file", "context/../open-questions.md"]

        text_run = runner.invoke(app, [*argv, "--format", "text"])
        assert text_run.exit_code == 0, text_run.output
        assert "Resolved section elided" in text_run.stdout

        json_run = runner.invoke(app, [*argv, "--format", "json"])
        assert json_run.exit_code == 0, json_run.output
        assert json.loads(json_run.stdout)["content"] == _OQ_CONTENT

    def test_l2_guard_in_text_mode_json_stays_uncapped(self, bounded_repo: Path):
        (bounded_repo / "state_history.md").write_text(("h" * 99 + "\n") * 2_500)

        text_run = runner.invoke(app, ["get-context", "--level", "L2", "--format", "text"])
        assert text_run.exit_code == 0, text_run.output
        assert "Context withheld" in text_run.stdout
        assert "(level L2)" in text_run.stdout

        json_run = runner.invoke(app, ["get-context", "--level", "L2", "--format", "json"])
        assert json_run.exit_code == 0, json_run.output
        assert len(json.loads(json_run.stdout)["content"]) > 200_000
