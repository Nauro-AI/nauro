"""Block-shape contract for the local stdio MCP read tools.

Read tools listed in ``nauro_core.renderers.RENDERERS`` must return two
``TextContent`` blocks: a human-formatted summary at ``[0]`` and the
JSON envelope at ``[1]``. Write tools, ``get_raw_file``,
``diff_since_last_session``, and pre-resolution error responses stay
single-block (or stay strings).

Mirrors ``TestContentBlockShape`` from the remote MCP router so both
transports stay in sync.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.types import TextContent
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations.propose_decision import _get_pending_store

from nauro.mcp.stdio_server import (
    check_decision,
    confirm_decision,
    diff_since_last_session,
    flag_question,
    get_context,
    get_decision,
    get_raw_file,
    list_decisions,
    propose_decision,
    search_decisions,
    update_state,
)
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


@pytest.fixture(autouse=True)
def _clear_pending():
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()


@pytest.fixture
def seeded_store(tmp_path: Path, monkeypatch) -> Path:
    """Pre-scaffolded project store with one decision and one question."""
    store_path = register_project("blockshape", [tmp_path / "repo"])
    scaffold_project_store("blockshape", store_path)
    (store_path / "stack.md").write_text(
        "# Stack\n- **Python 3.11** — primary language\n- **FastAPI** — HTTP framework\n"
    )
    append_decision(
        store_path,
        "Use FastAPI",
        rationale="FastAPI plus Mangum is the Lambda deployment combination.",
    )
    _flag_question_op(FilesystemStore(store_path), "Should we add caching?", None)
    return store_path


class TestTwoBlockReadTools:
    """Renderer-scoped read tools return ``[human, json]`` content blocks."""

    def test_get_context_returns_two_blocks(self, seeded_store: Path):
        blocks = get_context(project_id="blockshape", level=0)
        assert len(blocks) == 2
        assert all(isinstance(b, TextContent) and b.type == "text" for b in blocks)
        envelope = json.loads(blocks[1].text)
        assert envelope["store"] == "local"
        # Local stdio uses `content` (kernel GetContextResult field name);
        # remote uses `context`. The shared renderer accepts both.
        assert "content" in envelope
        # Human block carries the L0 markdown headers verbatim.
        assert "## Current State" in blocks[0].text

    def test_list_decisions_returns_two_blocks(self, seeded_store: Path):
        blocks = list_decisions(project_id="blockshape")
        assert len(blocks) == 2
        envelope = json.loads(blocks[1].text)
        assert envelope["store"] == "local"
        assert "decisions" in envelope
        # Human block carries the D001 label for the seeded decision.
        assert "D001" in blocks[0].text

    def test_get_decision_returns_two_blocks(self, seeded_store: Path):
        # scaffold_project_store seeds D001 (initial-setup); the fixture
        # appends D002 = "Use FastAPI". Ask for D002 so the title
        # assertion is independent of the scaffold's seed text.
        blocks = get_decision(number=2, project_id="blockshape")
        assert len(blocks) == 2
        envelope = json.loads(blocks[1].text)
        assert envelope["store"] == "local"
        assert "content" in envelope
        # Title surfaces in the human block.
        assert "Use FastAPI" in blocks[0].text

    def test_search_decisions_returns_two_blocks(self, seeded_store: Path):
        blocks = search_decisions(query="FastAPI", project_id="blockshape")
        assert len(blocks) == 2
        envelope = json.loads(blocks[1].text)
        assert envelope["store"] == "local"
        assert "results" in envelope
        # Human block echoes the query and surfaces the D### label.
        assert "FastAPI" in blocks[0].text

    def test_check_decision_returns_two_blocks(self, seeded_store: Path):
        blocks = check_decision(
            proposed_approach="Use FastAPI with async endpoints for the API server",
            project_id="blockshape",
        )
        assert len(blocks) == 2
        envelope = json.loads(blocks[1].text)
        assert envelope["store"] == "local"
        assert "related_decisions" in envelope


class TestSingleBlockReads:
    """Pass-through read tools keep the single-value shape — ``get_raw_file``
    returns a dict (no renderer registered), ``diff_since_last_session`` likewise."""

    def test_get_raw_file_returns_dict(self, seeded_store: Path):
        result = get_raw_file(path="stack.md", project_id="blockshape")
        # No renderer scope; the FastMCP layer converts the dict to its own
        # single content block at the MCP wire boundary. Direct Python
        # callers see the dict envelope unchanged.
        assert isinstance(result, dict)
        assert result["content"].startswith("# Stack")

    def test_diff_since_last_session_returns_dict(self, seeded_store: Path):
        result = diff_since_last_session(project_id="blockshape")
        assert isinstance(result, dict)


class TestSingleBlockWrites:
    """Write tools stay single-block; renderer scope is read-only."""

    def test_propose_decision_returns_dict(self, seeded_store: Path):
        result = propose_decision(
            project_id="blockshape",
            title="Adopt Postgres",
            rationale="ACID compliance trumps document flexibility for this workload.",
        )
        assert isinstance(result, dict)
        assert "status" in result

    def test_confirm_decision_returns_dict(self, seeded_store: Path):
        # An invalid id surfaces an error envelope, still a dict.
        result = confirm_decision(confirm_id="nonexistent", project_id="blockshape")
        assert isinstance(result, dict)

    def test_flag_question_returns_string(self, seeded_store: Path):
        result = flag_question(question="Need WebSocket?", project_id="blockshape")
        # flag_question intentionally returns a string for FastMCP
        # compatibility; the wrapper here is unchanged by the renderer work.
        assert isinstance(result, str)

    def test_update_state_returns_string(self, seeded_store: Path):
        result = update_state(delta="Shipped block-shape coverage", project_id="blockshape")
        assert isinstance(result, str)


class TestErrorAndFallbackPaths:
    """Renderer-scoped tools that hit a structured error envelope still
    emit two blocks; renderer failures fall back to JSON-only."""

    def test_overlong_check_decision_keeps_two_blocks(self, seeded_store: Path):
        from nauro_core.constants import MAX_APPROACH_LENGTH

        overlong = "x" * (MAX_APPROACH_LENGTH + 1)
        blocks = check_decision(proposed_approach=overlong, project_id="blockshape")
        assert len(blocks) == 2
        # Human block leads with the Error: header.
        assert blocks[0].text.startswith("Error:")
        envelope = json.loads(blocks[1].text)
        assert envelope["error"]["kind"] == "rejected"

    def test_renderer_failure_falls_back_to_json_only(self, seeded_store: Path, monkeypatch):
        """If a renderer raises unexpectedly, the wrapper must not lose
        the response; it falls back to a single JSON block so programmatic
        consumers still get a parseable envelope."""
        import nauro.mcp.stdio_server as stdio_mod

        def explode(_result):
            raise RuntimeError("renderer kaboom")

        monkeypatch.setitem(stdio_mod._RENDERERS, "list_decisions", explode)
        blocks = list_decisions(project_id="blockshape")
        assert len(blocks) == 1
        envelope = json.loads(blocks[0].text)
        assert envelope["store"] == "local"
        assert "decisions" in envelope

    def test_pre_resolution_error_still_two_blocks_on_renderer_scope(self, seeded_store: Path):
        """Store-resolution failures inside a renderer-scoped tool flow
        through the renderer — the human block carries the kernel's
        guidance string and the JSON block carries the error envelope."""
        # Unknown project_id triggers StoreResolutionError; the function
        # still wraps the error dict via the renderer (Error: ... header).
        blocks = get_context(project_id="does-not-exist")
        assert len(blocks) == 2
        envelope = json.loads(blocks[1].text)
        assert envelope["store"] == "local"
        assert envelope["status"] == "error"
        assert "guidance" in envelope
