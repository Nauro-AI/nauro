"""Tests for empty state handling in MCP tools.

Covers two scenarios for each tool:
1. No store (store_path doesn't exist) — should return onboarding guidance.
2. Empty store (store exists but no decisions) — tools that read decisions
   should return appropriate guidance.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from nauro.mcp.tools import (
    tool_check_decision,
    tool_flag_question,
    tool_get_context,
    tool_propose_decision,
    tool_update_state,
)
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture()
def nonexistent_store(tmp_path: Path) -> Path:
    """A store path that does not exist."""
    return tmp_path / "projects" / "nonexistent"


@pytest.fixture()
def empty_store(tmp_path: Path) -> Path:
    """A scaffolded store with the initial decision removed (no decisions)."""
    store_path = tmp_path / "projects" / "emptyproj"
    scaffold_project_store("emptyproj", store_path)
    # Remove the scaffolded first decision to get a truly empty decisions dir
    for f in (store_path / "decisions").glob("*.md"):
        f.unlink()
    return store_path


# ── No store tests ──


class TestNoStore:
    def test_get_context_returns_guidance(self, nonexistent_store):
        result = tool_get_context(nonexistent_store, 0)
        assert result["status"] == "error"
        assert "nauro init" in result["guidance"]
        assert "Welcome" in result["guidance"]

    def test_propose_decision_returns_guidance(self, nonexistent_store):
        result = tool_propose_decision(nonexistent_store, title="Test", rationale="Testing")
        assert result["status"] == "error"
        assert "nauro init" in result["guidance"]

    def test_check_decision_returns_guidance(self, nonexistent_store):
        # Missing-store path is the transport's responsibility — the kernel
        # operation never sees this case, so the legacy envelope shape stays.
        result = tool_check_decision(nonexistent_store, "Use Redis")
        assert result["status"] == "error"
        assert "nauro init" in result["guidance"]

    def test_flag_question_returns_guidance(self, nonexistent_store):
        result = tool_flag_question(nonexistent_store, "Should we use gRPC?")
        assert result["status"] == "error"
        assert "nauro init" in result["guidance"]

    def test_update_state_returns_guidance(self, nonexistent_store):
        result = tool_update_state(nonexistent_store, "Deployed v1.0")
        assert result["status"] == "error"
        assert "nauro init" in result["guidance"]


# ── Empty store tests (store exists, no decisions) ──


class TestEmptyStore:
    def test_get_context_includes_no_context_guidance(self, empty_store):
        result = tool_get_context(empty_store, 0)
        content = result["content"]
        assert "no context data yet" in content or "propose_decision" in content

    def test_check_decision_returns_no_decisions_guidance(self, empty_store):
        result = tool_check_decision(empty_store, "Use Redis")
        assert result["related_decisions"] == []
        assert "No existing decisions" in result["assessment"]

    def test_propose_decision_works_on_empty_store(self, empty_store):
        """First decision should work normally — not blocked by empty state."""
        with patch("nauro.mcp.tools._try_push"):
            result = tool_propose_decision(
                empty_store,
                title="Use Postgres for primary storage",
                rationale="ACID compliance for the transactional workload across the platform.",
            )
        assert result["status"] == "confirmed"

    def test_flag_question_works_on_empty_store(self, empty_store):
        """Questions should work fine even with no decisions."""
        with patch("nauro.mcp.tools._try_push"):
            result = tool_flag_question(empty_store, "Should we use gRPC?")
        assert result["status"] == "ok"

    def test_update_state_works_on_empty_store(self, empty_store):
        """State updates should work fine with no decisions."""
        with patch("nauro.mcp.tools._try_push"):
            result = tool_update_state(empty_store, "Deployed v1.0")
        assert result["status"] == "ok"
