"""Tests for auto-push after MCP tool writes (confirm_decision, flag_question, update_state)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from nauro.mcp.tools import tool_confirm_decision, tool_flag_question, tool_update_state
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


class TestConfirmDecisionAutoPush:
    def test_sync_triggers_after_confirm(self, store):
        """push_after_write is called when confirm_write succeeds."""
        fake_confirm_result = {
            "status": "confirmed",
            "decision_id": "decision-001",
            "title": "Use Redis",
            "operation": "add",
        }
        with (
            patch("nauro.mcp.tools.confirm_write", return_value=fake_confirm_result),
            patch("nauro.mcp.tools._try_push") as mock_push,
        ):
            result = tool_confirm_decision(store, "abc123")

        assert result["status"] == "confirmed"
        mock_push.assert_called_once_with(store)

    def test_sync_not_called_on_error(self, store):
        """push_after_write is NOT called when confirm_write returns an error."""
        fake_error_result = {"error": "Invalid or expired confirm_id."}
        with (
            patch("nauro.mcp.tools.confirm_write", return_value=fake_error_result),
            patch("nauro.mcp.tools._try_push") as mock_push,
        ):
            result = tool_confirm_decision(store, "bad-id")

        assert "error" in result
        mock_push.assert_not_called()

    def test_sync_failure_does_not_block_confirm(self, store):
        """If push_after_write raises, the confirmation result is still returned."""
        fake_confirm_result = {
            "status": "confirmed",
            "decision_id": "decision-001",
            "title": "Use Redis",
            "operation": "add",
        }
        with (
            patch("nauro.mcp.tools.confirm_write", return_value=fake_confirm_result),
            patch(
                "nauro.sync.hooks.push_after_write",
                side_effect=Exception("S3 down"),
            ),
        ):
            result = tool_confirm_decision(store, "abc123")

        assert result["status"] == "confirmed"
        assert result["decision_id"] == "decision-001"


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
