"""Tests for Tier 3 LLM evaluation."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nauro.store.writer import append_decision
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.tier3 import check_conflicts_with_llm, evaluate_with_llm


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    append_decision(
        store_path,
        "Use Postgres",
        rationale="Better JSON support and mature ecosystem for our needs.",
        confidence="high",
        decision_type="data_model",
    )
    return store_path


def _make_mock_response(tool_name: str, tool_input: dict):
    """Create a mock Anthropic response with a tool use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input

    response = MagicMock()
    response.content = [block]
    return response


class TestEvaluateWithLlm:
    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_add_operation(self, mock_anthropic_cls, store):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "evaluate_decision",
            {
                "operation": "add",
                "assessment": "This is a new decision about caching.",
                "conflicts": [],
                "suggested_refinements": None,
                "affected_decision_id": None,
            },
        )

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.7}]
        result = evaluate_with_llm(
            {"title": "Use Redis for Caching", "rationale": "Need fast session store."},
            similar,
            store,
        )
        assert result["operation"] == "add"
        assert result["conflicts"] == []

    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_supersede_operation(self, mock_anthropic_cls, store):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "evaluate_decision",
            {
                "operation": "supersede",
                "assessment": "This replaces the Postgres decision with MySQL.",
                "conflicts": [
                    {
                        "decision_id": "decision-002",
                        "conflict_description": "Directly replaces database choice.",
                    }
                ],
                "suggested_refinements": None,
                "affected_decision_id": "decision-002",
            },
        )

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.85}]
        result = evaluate_with_llm(
            {"title": "Switch to MySQL", "rationale": "Cost reduction needed."},
            similar,
            store,
        )
        assert result["operation"] == "supersede"
        assert result["affected_decision_id"] == "decision-002"
        assert len(result["conflicts"]) == 1

    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_update_operation(self, mock_anthropic_cls, store):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "evaluate_decision",
            {
                "operation": "update",
                "assessment": "This adds connection pooling details to the Postgres decision.",
                "conflicts": [],
                "suggested_refinements": None,
                "affected_decision_id": "decision-002",
            },
        )

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.78}]
        result = evaluate_with_llm(
            {
                "title": "Configure Postgres Connection Pool",
                "rationale": "Use pgbouncer with 20 connections.",
            },
            similar,
            store,
        )
        assert result["operation"] == "update"
        assert result["affected_decision_id"] == "decision-002"

    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_noop_operation(self, mock_anthropic_cls, store):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "evaluate_decision",
            {
                "operation": "noop",
                "assessment": "Already captured by decision-002.",
                "conflicts": [],
            },
        )

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.95}]
        result = evaluate_with_llm(
            {"title": "Use Postgres", "rationale": "Better JSON support."},
            similar,
            store,
        )
        assert result["operation"] == "noop"

    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_returns_hold_on_api_failure(self, mock_anthropic_cls, store):
        """When LLM call fails, operation is 'hold' — not 'add' (fail-closed)."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("API unavailable")

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.7}]
        result = evaluate_with_llm(
            {"title": "Use Redis", "rationale": "For caching purposes in production."},
            similar,
            store,
        )
        assert result["operation"] == "hold", (
            "LLM failure must return 'hold', not 'add', so similar decisions "
            "are not blindly written without conflict checking."
        )
        assert "unavailable" in result["assessment"].lower()

    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_api_key_forwarded_to_anthropic_client(self, mock_anthropic_cls, store):
        """Explicit api_key must be passed to Anthropic() constructor."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "evaluate_decision",
            {"operation": "add", "assessment": "ok", "conflicts": []},
        )

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.7}]
        evaluate_with_llm(
            {"title": "Use Redis", "rationale": "For caching."},
            similar,
            store,
            api_key="sk-test-key-123",
        )
        mock_anthropic_cls.assert_called_once_with(api_key="sk-test-key-123")


class TestCheckConflicts:
    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_finds_conflicts(self, mock_anthropic_cls, store):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "check_conflicts",
            {
                "related_decisions": [{"decision_id": "decision-002", "relevance": "high"}],
                "potential_conflicts": [
                    {
                        "decision_id": "decision-002",
                        "conflict": "Switching to MySQL contradicts the Postgres decision.",
                    }
                ],
                "assessment": "Direct conflict with existing database choice.",
            },
        )

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.8}]
        result = check_conflicts_with_llm(
            "Switch to MySQL for cost savings",
            None,
            similar,
            store,
        )
        assert len(result["potential_conflicts"]) == 1
        assert "MySQL" in result["potential_conflicts"][0]["conflict"]

    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_returns_empty_on_api_failure(self, mock_anthropic_cls, store):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("API unavailable")

        similar = [{"id": "decision-002", "title": "Use Postgres", "similarity": 0.8}]
        result = check_conflicts_with_llm(
            "Use a graph database",
            None,
            similar,
            store,
        )
        assert "unavailable" in result["assessment"].lower()
