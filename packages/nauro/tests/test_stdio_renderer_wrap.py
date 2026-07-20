"""Block-shape contract for the local stdio MCP read tools.

Renderer-scoped read tools listed in ``nauro_core.renderers.RENDERERS``
return a ``CallToolResult`` with a single ``TextContent`` block:
``content[0]`` carries the renderer output. Write tools,
``get_raw_file``, ``diff_since_last_session``, and pre-resolution error
responses stay single-block (or stay strings).

Mirrors ``TestContentBlockShape`` from the remote MCP router so both
transports stay in sync.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent
from nauro_core.operations import flag_question as _flag_question_op

from nauro.mcp.stdio_server import (
    check_decision,
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
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


@pytest.fixture
def seeded_store(tmp_path: Path, monkeypatch) -> Path:
    """Pre-scaffolded project store with one decision and one question."""
    _pid, store_path = register_project_v2("blockshape", [tmp_path / "repo"])
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


class TestSingleBlockReadTools:
    """Renderer-scoped read tools return a single rendered ``content[0]`` block."""

    def test_get_context_returns_single_block(self, seeded_store: Path):
        result = get_context(project_id="blockshape", level=0)
        assert isinstance(result, CallToolResult)
        blocks = result.content
        assert len(blocks) == 1
        assert isinstance(blocks[0], TextContent) and blocks[0].type == "text"
        # Rendered block carries the L0 markdown headers verbatim.
        assert "## Current State" in blocks[0].text

    def test_list_decisions_returns_single_block(self, seeded_store: Path):
        result = list_decisions(project_id="blockshape")
        assert isinstance(result, CallToolResult)
        blocks = result.content
        assert len(blocks) == 1
        # Rendered block carries the D001 label for the seeded decision.
        assert "D001" in blocks[0].text

    def test_get_decision_returns_single_block(self, seeded_store: Path):
        # scaffold_project_store seeds D001 (initial-setup); the fixture
        # appends D002 = "Use FastAPI". Ask for D002 so the title
        # assertion is independent of the scaffold's seed text.
        result = get_decision(number=2, project_id="blockshape")
        assert isinstance(result, CallToolResult)
        blocks = result.content
        assert len(blocks) == 1
        # Title surfaces in the rendered block.
        assert "Use FastAPI" in blocks[0].text

    def test_search_decisions_returns_single_block(self, seeded_store: Path):
        result = search_decisions(query="FastAPI", project_id="blockshape")
        assert isinstance(result, CallToolResult)
        blocks = result.content
        assert len(blocks) == 1
        # The header echoes the caller's query. Asserting the exact header
        # substring (not just "FastAPI" anywhere) guards the kernel-envelope
        # prune of the echoed query: the term also appears in the matched
        # decision's title, so a bare membership check passes even when the
        # header renders the empty string. ``for "FastAPI"`` only appears when
        # the query is threaded through to the renderer.
        assert 'for "FastAPI"' in blocks[0].text

    def test_check_decision_returns_single_block(self, seeded_store: Path):
        result = check_decision(
            proposed_approach="Use FastAPI with async endpoints for the API server",
            project_id="blockshape",
        )
        assert isinstance(result, CallToolResult)
        blocks = result.content
        assert len(blocks) == 1
        # Renderer emits a "top match" / BM25 line when at least one hit lands.
        assert "top match" in blocks[0].text or "BM25" in blocks[0].text


class TestNoStructuredContent:
    """Healthy renderer-scoped reads stay single-block text responses."""

    def test_get_context_has_no_structured_content(self, seeded_store: Path):
        result = get_context(project_id="blockshape", level=0)
        assert result.structuredContent is None

    def test_list_decisions_has_no_structured_content(self, seeded_store: Path):
        result = list_decisions(project_id="blockshape")
        assert result.structuredContent is None

    def test_get_decision_has_no_structured_content(self, seeded_store: Path):
        result = get_decision(number=2, project_id="blockshape")
        assert result.structuredContent is None

    def test_search_decisions_has_no_structured_content(self, seeded_store: Path):
        result = search_decisions(query="FastAPI", project_id="blockshape")
        assert result.structuredContent is None

    def test_check_decision_has_no_structured_content(self, seeded_store: Path):
        result = check_decision(
            proposed_approach="Use FastAPI with async endpoints for the API server",
            project_id="blockshape",
        )
        assert result.structuredContent is None


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (get_context, {"level": 0}),
        (list_decisions, {}),
        (get_decision, {"number": 1}),
        (search_decisions, {"query": "typed recovery"}),
        (check_decision, {"proposed_approach": "Use typed recovery"}),
    ],
)
def test_disconnected_renderer_reads_preserve_full_structured_envelope(
    tool, kwargs, tmp_path, monkeypatch
):
    repo = tmp_path / "disconnected"
    repo.mkdir()
    project_id = "01KQ6AZGNA0B3QBF67NBXP3S45"
    save_repo_config(repo, {"mode": "local", "id": project_id, "name": "Pareto"})

    result = tool(cwd=str(repo), **kwargs)

    assert isinstance(result, CallToolResult)
    assert result.structuredContent is not None
    assert set(result.structuredContent) == {
        "store",
        "status",
        "error",
        "guidance",
        "project_id",
        "project_name",
        "project_mode",
        "reason_code",
        "recovery_actions",
    }
    assert result.structuredContent["store"] == "local"
    assert result.structuredContent["status"] == "error"
    assert result.structuredContent["error"] == {
        "kind": "error",
        "reason": result.structuredContent["guidance"],
    }
    assert result.structuredContent["project_id"] == project_id
    assert result.structuredContent["project_name"] == "Pareto"
    assert result.structuredContent["project_mode"] == "local"
    assert result.structuredContent["reason_code"] == "not_connected_on_this_machine"
    assert result.structuredContent["recovery_actions"] == ["locate", "continue"]
    assert result.content[0].text == result.structuredContent["guidance"]


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

    def test_flag_question_returns_string(self, seeded_store: Path):
        result = flag_question(question="Need WebSocket?", project_id="blockshape")
        # flag_question intentionally returns a string for FastMCP
        # compatibility; the wrapper here is unchanged by the renderer work.
        assert isinstance(result, str)

    def test_update_state_returns_string(self, seeded_store: Path):
        result = update_state(delta="Shipped block-shape coverage", project_id="blockshape")
        assert isinstance(result, str)


class TestErrorAndFallbackPaths:
    """Renderer-scoped tools keep the single-block shape on structured
    error envelopes and renderer failures alike."""

    def test_overlong_check_decision_keeps_single_block(self, seeded_store: Path):
        from nauro_core.constants import MAX_APPROACH_LENGTH

        overlong = "x" * (MAX_APPROACH_LENGTH + 1)
        result = check_decision(proposed_approach=overlong, project_id="blockshape")
        blocks = result.content
        assert len(blocks) == 1
        # Rendered block leads with the Error: header.
        assert blocks[0].text.startswith("Error:")
        assert result.structuredContent is None

    def test_renderer_failure_falls_back_to_json_only(self, seeded_store: Path, monkeypatch):
        """If a renderer raises unexpectedly, the wrapper must not lose
        the response; it falls back to a single JSON block so programmatic
        consumers still get a parseable envelope."""
        import nauro.mcp.stdio_server as stdio_mod

        def explode(_result):
            raise RuntimeError("renderer kaboom")

        monkeypatch.setitem(stdio_mod._RENDERERS, "list_decisions", explode)
        result = list_decisions(project_id="blockshape")
        blocks = result.content
        assert len(blocks) == 1
        envelope = json.loads(blocks[0].text)
        assert envelope["store"] == "local"
        assert "decisions" in envelope
        assert result.structuredContent is None

    def test_pre_resolution_error_still_single_block_on_renderer_scope(self, seeded_store: Path):
        """Store-resolution failures inside a renderer-scoped tool flow
        through the renderer wrapper and surface as a single-block shape
        with no ``structuredContent``."""
        # Unknown project_id triggers StoreResolutionError; the function
        # still wraps the error dict via the renderer.
        result = get_context(project_id="does-not-exist")
        blocks = result.content
        assert len(blocks) == 1
        assert result.structuredContent is None
