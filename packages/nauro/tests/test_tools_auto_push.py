"""Tests for auto-push after MCP tool writes (propose_decision, flag_question, update_state)."""

from unittest.mock import patch

from nauro.mcp.tools import tool_flag_question, tool_propose_decision, tool_update_state


class TestProposeDecisionAutoPush:
    def test_sync_triggers_after_confirmed_propose(self, scaffolded_store):
        """push_after_write is called when propose_decision commits."""
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_propose_decision(
                scaffolded_store,
                title="Use Redis for hot caching",
                rationale=(
                    "In-memory cache for hot read paths across the API tier and pub/sub channels."
                ),
                confidence="medium",
            )

        assert result["status"] == "confirmed"
        mock_push.assert_called_once_with(scaffolded_store)

    def test_sync_not_called_on_rejection(self, scaffolded_store):
        """push_after_write is NOT called when the kernel rejects the proposal."""
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_propose_decision(
                scaffolded_store,
                title="",
                rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
                confidence="medium",
            )

        assert result["status"] == "rejected"
        mock_push.assert_not_called()

    def test_sync_failure_does_not_block_propose(self, scaffolded_store):
        """If push_after_write raises, the propose result is still returned."""
        with patch(
            "nauro.sync.hooks.push_after_write",
            side_effect=Exception("S3 down"),
        ):
            result = tool_propose_decision(
                scaffolded_store,
                title="Use Redis for hot caching",
                rationale=(
                    "In-memory cache for hot read paths across the API tier and pub/sub channels."
                ),
                confidence="medium",
            )

        assert result["status"] == "confirmed"
        assert result["decision_id"]


class TestFlagQuestionAutoPush:
    def test_sync_triggers_after_flag_question(self, scaffolded_store):
        """push_after_write is called after flagging a question."""
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_flag_question(scaffolded_store, "Should we use gRPC?")

        assert result["status"] == "ok"
        mock_push.assert_called_once_with(scaffolded_store)

    def test_sync_failure_does_not_block_flag_question(self, scaffolded_store):
        """If push raises, the question is still flagged successfully."""
        with patch(
            "nauro.sync.hooks.push_after_write",
            side_effect=Exception("S3 down"),
        ):
            result = tool_flag_question(scaffolded_store, "Should we use gRPC?")

        assert result["status"] == "ok"


class TestUpdateStateAutoPush:
    def test_sync_triggers_after_update_state(self, scaffolded_store):
        """push_after_write is called after updating state."""
        with patch("nauro.mcp.tools._try_push") as mock_push:
            result = tool_update_state(scaffolded_store, "Deployed v1.0")

        assert result["status"] == "ok"
        mock_push.assert_called_once_with(scaffolded_store)

    def test_sync_failure_does_not_block_update_state(self, scaffolded_store):
        """If push raises, the state update still succeeds."""
        with patch(
            "nauro.sync.hooks.push_after_write",
            side_effect=Exception("S3 down"),
        ):
            result = tool_update_state(scaffolded_store, "Deployed v1.0")

        assert result["status"] == "ok"
