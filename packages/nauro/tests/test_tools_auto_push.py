"""Tests for auto-push after MCP tool writes (confirm_decision, flag_question, update_state)."""

from pathlib import Path
from unittest.mock import patch

import pytest
from nauro_core.operations.propose_decision import _get_pending_store

from nauro.mcp.tools import tool_confirm_decision, tool_flag_question, tool_update_state
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


@pytest.fixture(autouse=True)
def _clear_pending():
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()


def _seed_pending_add(title: str = "Use Redis for hot caching") -> str:
    """Seed a pending ``add`` entry the kernel can replay on confirm."""
    return _get_pending_store().store(
        {
            "proposal": {
                "title": title,
                "rationale": (
                    "In-memory cache for hot read paths across the API tier and pub/sub channels."
                ),
                "confidence": "medium",
            },
            "operation": "add",
            "affected_decision_id": None,
        },
        {"tier": 1, "operation": "add", "similar_decisions": [], "assessment": "seed"},
    )


class TestConfirmDecisionAutoPush:
    def test_sync_triggers_after_confirm(self, store):
        """push_after_write is called when the kernel confirms the pending entry."""
        confirm_id = _seed_pending_add()
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_confirm_decision(store, confirm_id)

        assert result["status"] == "confirmed"
        mock_push.assert_called_once_with(store)

    def test_sync_not_called_on_error(self, store):
        """push_after_write is NOT called when the confirm_id is unknown."""
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_confirm_decision(store, "bad-id")

        assert result["status"] == "rejected"
        assert result["error"]["kind"] == "rejected"
        mock_push.assert_not_called()

    def test_sync_failure_does_not_block_confirm(self, store):
        """If push_after_write raises, the confirmation result is still returned."""
        confirm_id = _seed_pending_add()
        with patch(
            "nauro.sync.hooks.push_after_write",
            side_effect=Exception("S3 down"),
        ):
            result = tool_confirm_decision(store, confirm_id)

        assert result["status"] == "confirmed"
        assert result["decision_id"]


class TestFlagQuestionAutoPush:
    def test_sync_triggers_after_flag_question(self, store):
        """push_after_write is called after flagging a question."""
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_flag_question(store, "Should we use gRPC?")

        assert result["status"] == "ok"
        mock_push.assert_called_once_with(store)

    def test_sync_failure_does_not_block_flag_question(self, store):
        """If push raises, the question is still flagged successfully."""
        with patch(
            "nauro.sync.hooks.push_after_write",
            side_effect=Exception("S3 down"),
        ):
            result = tool_flag_question(store, "Should we use gRPC?")

        assert result["status"] == "ok"


class TestUpdateStateAutoPush:
    def test_sync_triggers_after_update_state(self, store):
        """push_after_write is called after updating state."""
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_update_state(store, "Deployed v1.0")

        assert result["status"] == "ok"
        mock_push.assert_called_once_with(store)

    def test_sync_failure_does_not_block_update_state(self, store):
        """If push raises, the state update still succeeds."""
        with patch(
            "nauro.sync.hooks.push_after_write",
            side_effect=Exception("S3 down"),
        ):
            result = tool_update_state(store, "Deployed v1.0")

        assert result["status"] == "ok"
