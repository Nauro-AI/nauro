"""Tests for the full validation pipeline."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nauro.store.writer import append_decision
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.pending import clear_all
from nauro.validation.pipeline import (
    confirm_write,
    validate_proposed_write,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


@pytest.fixture(autouse=True)
def _clear_pending():
    """Clear pending store between tests."""
    clear_all()
    yield
    clear_all()


class TestTier1Rejection:
    def test_rejects_empty_title(self, store):
        proposal = {"title": "", "rationale": "Some valid rationale text here."}
        result = validate_proposed_write(proposal, store)
        assert result.status == "rejected"
        assert result.tier == 1

    def test_rejects_short_rationale(self, store):
        proposal = {"title": "Good Title", "rationale": "Too short."}
        result = validate_proposed_write(proposal, store)
        assert result.status == "rejected"
        assert result.tier == 1


class TestAutoConfirmPath:
    """Test the auto_confirm=True path (extraction pipeline)."""

    def test_new_decision_auto_confirms(self, store):
        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store with pub/sub support for session data.",
            "confidence": "high",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=True)
        assert result.status == "confirmed"
        assert result.tier == 2
        assert result.operation == "add"

    def test_auto_confirm_writes_decision_file(self, store):
        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store with pub/sub for sessions and invalidation.",
            "confidence": "high",
            "decision_type": "infrastructure",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=True)
        assert result.status == "confirmed"

        # Verify the file was written
        decisions_dir = store / "decisions"
        decision_files = list(decisions_dir.glob("*redis*.md"))
        assert len(decision_files) >= 1


class TestMCPPath:
    """Test the auto_confirm=False path (MCP tools)."""

    def test_new_decision_auto_confirms_even_mcp(self, store):
        """When Tier 2 finds no similar decisions, auto-confirm even for MCP."""
        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store with pub/sub support for session management.",
            "confidence": "high",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=False)
        assert result.status == "confirmed"
        assert result.confirm_id is None  # No pending needed

    @patch("nauro.validation.pipeline.check_similarity")
    @patch("nauro.validation.pipeline.evaluate_with_llm")
    def test_similar_decision_returns_pending(self, mock_llm, mock_sim, store):
        """When similar decisions found, return pending_confirmation for MCP."""
        mock_sim.return_value = (
            "needs_review",
            [{"id": "decision-001", "title": "Initial Setup", "similarity": 0.75}],
        )
        mock_llm.return_value = {
            "operation": "add",
            "assessment": "Different enough to add.",
            "suggested_refinements": None,
            "conflicts": [],
            "affected_decision_id": None,
        }

        proposal = {
            "title": "Use Postgres",
            "rationale": "Better JSON support for our application layer.",
            "confidence": "high",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=False)
        assert result.status == "pending_confirmation"
        assert result.confirm_id is not None
        assert result.tier == 3

    @patch("nauro.validation.pipeline.check_similarity")
    @patch("nauro.validation.pipeline.evaluate_with_llm")
    def test_confirm_writes_decision(self, mock_llm, mock_sim, store):
        """Confirming a pending proposal writes the decision."""
        mock_sim.return_value = (
            "needs_review",
            [{"id": "decision-001", "title": "Initial Setup", "similarity": 0.75}],
        )
        mock_llm.return_value = {
            "operation": "add",
            "assessment": "New decision.",
            "suggested_refinements": None,
            "conflicts": [],
            "affected_decision_id": None,
        }

        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store for session data management.",
            "confidence": "high",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=False)
        assert result.status == "pending_confirmation"

        confirm_result = confirm_write(result.confirm_id, store)
        assert confirm_result["status"] == "confirmed"
        assert "decision_id" in confirm_result

    @patch("nauro.validation.pipeline.check_similarity")
    @patch("nauro.validation.pipeline.evaluate_with_llm")
    def test_noop_skips_write(self, mock_llm, mock_sim, store):
        """NOOP from LLM skips the write."""
        mock_sim.return_value = (
            "needs_review",
            [{"id": "decision-001", "title": "Initial Setup", "similarity": 0.9}],
        )
        mock_llm.return_value = {
            "operation": "noop",
            "assessment": "Already captured.",
            "suggested_refinements": None,
            "conflicts": [],
        }

        proposal = {
            "title": "Initial Setup Again",
            "rationale": "Same as the initial project setup decision.",
            "confidence": "medium",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=False)
        assert result.status == "noop"
        assert result.operation == "noop"


class TestAutoConfirmWithSimilarity:
    """Test auto_confirm=True with Tier 3 operations."""

    @patch("nauro.validation.pipeline.check_similarity")
    @patch("nauro.validation.pipeline.evaluate_with_llm")
    def test_auto_confirm_supersede(self, mock_llm, mock_sim, store):
        # First write a decision to supersede
        append_decision(store, "Use MySQL", rationale="Cheap and widely available database option.")

        mock_sim.return_value = (
            "needs_review",
            [{"id": "decision-002", "title": "Use MySQL", "similarity": 0.85}],
        )
        mock_llm.return_value = {
            "operation": "supersede",
            "assessment": "Replaces MySQL with Postgres.",
            "conflicts": [],
            "affected_decision_id": "002-use-mysql",
        }

        proposal = {
            "title": "Switch to Postgres",
            "rationale": "Better JSON support needed for our application.",
            "confidence": "high",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=True)
        assert result.status == "confirmed"
        assert result.operation == "supersede"


class TestConfirmWrite:
    def test_invalid_confirm_id(self, store):
        result = confirm_write("nonexistent-uuid", store)
        assert "error" in result

    @patch("nauro.validation.pipeline.check_similarity")
    @patch("nauro.validation.pipeline.evaluate_with_llm")
    def test_expired_confirm_id(self, mock_llm, mock_sim, store):
        """Expired confirm_ids return error."""
        from datetime import UTC, datetime, timedelta

        from nauro.validation.pending import _store

        mock_sim.return_value = (
            "needs_review",
            [{"id": "decision-001", "title": "Test", "similarity": 0.8}],
        )
        mock_llm.return_value = {
            "operation": "add",
            "assessment": "New.",
            "conflicts": [],
        }

        proposal = {
            "title": "Test Decision Here",
            "rationale": "Testing expiry behavior of pending proposals.",
            "confidence": "medium",
        }
        result = validate_proposed_write(proposal, store, auto_confirm=False)
        assert result.confirm_id is not None

        # Manually expire it
        _store._pending[result.confirm_id]["created_at"] = datetime.now(UTC) - timedelta(minutes=15)

        confirm_result = confirm_write(result.confirm_id, store)
        assert "error" in confirm_result


class TestFailClosedBehavior:
    """Tier 3 LLM failures must not silently auto-add decisions."""

    def _store_with_similar_decision(self, tmp_path: Path) -> Path:
        """Return a store that has a decision similar enough to trigger Tier 3."""
        store_path = tmp_path / "projects" / "fc-proj"
        scaffold_project_store("fc-proj", store_path)
        append_decision(
            store_path,
            "Use Postgres as primary database",
            rationale="Mature ecosystem, excellent JSON support, wide hosting options.",
            confidence="high",
            decision_type="data_model",
        )
        return store_path

    @patch("nauro.validation.pipeline.check_similarity")
    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_auto_confirm_path_holds_when_llm_fails(
        self, mock_anthropic_cls, mock_check_similarity, tmp_path
    ):
        """When LLM is unavailable, auto_confirm path must return 'held', not write."""
        store_path = self._store_with_similar_decision(tmp_path)

        # Tier 2 says "similar" so Tier 3 is invoked
        mock_check_similarity.return_value = (
            "review",
            [{"id": "decision-001", "similarity": 0.82, "title": "Use Postgres"}],
        )
        # LLM call fails
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("Connection refused")

        decisions_before = list((store_path / "decisions").glob("*.md"))

        result = validate_proposed_write(
            {
                "title": "Switch to MySQL",
                "rationale": "Lower licensing cost for our workload size requirements.",
            },
            store_path,
            auto_confirm=True,
        )

        assert result.status == "held", (
            f"Expected status='held' but got '{result.status}'. "
            "The decision should not be written when the LLM is unavailable."
        )
        assert result.operation == "hold"

        decisions_after = list((store_path / "decisions").glob("*.md"))
        assert decisions_before == decisions_after, (
            "No new decision file should be created when the LLM evaluation is held."
        )

    @patch("nauro.validation.pipeline.check_similarity")
    @patch("nauro.validation.tier3.anthropic.Anthropic")
    def test_mcp_path_queues_for_confirmation_when_llm_fails(
        self, mock_anthropic_cls, mock_check_similarity, tmp_path
    ):
        """MCP path (auto_confirm=False) with LLM failure returns pending_confirmation."""
        store_path = self._store_with_similar_decision(tmp_path)

        mock_check_similarity.return_value = (
            "review",
            [{"id": "decision-001", "similarity": 0.82, "title": "Use Postgres"}],
        )
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("Rate limited")

        result = validate_proposed_write(
            {
                "title": "Switch to MySQL",
                "rationale": "Lower licensing cost for our workload size requirements.",
            },
            store_path,
            auto_confirm=False,
        )

        # MCP path should queue for human review, not silently add
        assert result.status == "pending_confirmation"
        assert result.confirm_id is not None


class TestValidationLog:
    def test_log_created(self, store):
        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store for session data management.",
            "confidence": "high",
        }
        validate_proposed_write(proposal, store)

        log_path = store / "validation-log.jsonl"
        assert log_path.exists()
        content = log_path.read_text()
        assert "Use Redis" in content
