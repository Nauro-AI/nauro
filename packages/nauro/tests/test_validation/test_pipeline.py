"""Tests for the deterministic validation pipeline."""

from pathlib import Path

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


@pytest.fixture
def store_with_existing(tmp_path: Path) -> Path:
    """Store with one real decision so BM25 has a near-neighbour to surface."""
    store_path = tmp_path / "projects" / "withdec"
    scaffold_project_store("withdec", store_path)
    append_decision(
        store_path,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )
    return store_path


@pytest.fixture(autouse=True)
def _clear_pending():
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


class TestNoSimilarityWrites:
    """No Tier 2 hit → direct write."""

    def test_new_decision_writes(self, store):
        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store with pub/sub support for session data.",
            "confidence": "high",
        }
        result = validate_proposed_write(proposal, store)
        assert result.status == "confirmed"
        assert result.tier == 2
        assert result.operation == "add"
        assert result.confirm_id is None

    def test_writes_decision_file(self, store):
        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store with pub/sub for sessions and invalidation.",
            "confidence": "high",
            "decision_type": "infrastructure",
        }
        result = validate_proposed_write(proposal, store)
        assert result.status == "confirmed"

        decisions_dir = store / "decisions"
        decision_files = list(decisions_dir.glob("*redis*.md"))
        assert len(decision_files) >= 1


class TestSimilarityRoutesToPending:
    """Tier 2 hit → pending_confirmation, regardless of caller."""

    def test_similar_decision_returns_pending(self, store_with_existing):
        proposal = {
            "title": "Use Postgres as the data layer",
            "rationale": "Better JSON support than alternatives for our application data.",
            "confidence": "high",
        }
        result = validate_proposed_write(proposal, store_with_existing)
        assert result.status == "pending_confirmation"
        assert result.confirm_id is not None
        assert result.tier == 2
        assert len(result.similar_decisions) >= 1

    def test_confirm_writes_decision(self, store_with_existing):
        """Caller-supplied operation='add' flows through to confirm_write."""
        proposal = {
            "title": "Use Postgres for analytics warehouse",
            "rationale": "Same engine as primary store, simplifies data movement and ops.",
            "confidence": "high",
        }
        result = validate_proposed_write(
            proposal,
            store_with_existing,
            operation="add",
        )
        assert result.status == "pending_confirmation"

        confirm_result = confirm_write(result.confirm_id, store_with_existing)
        assert confirm_result["status"] == "confirmed"
        assert "decision_id" in confirm_result
        assert confirm_result["operation"] == "add"


class TestConfirmWrite:
    def test_invalid_confirm_id(self, store):
        result = confirm_write("nonexistent-uuid", store)
        assert "error" in result

    def test_expired_confirm_id(self, store_with_existing):
        from datetime import datetime, timedelta, timezone

        from nauro.validation.pending import _store

        proposal = {
            "title": "Use Postgres for read replicas",
            "rationale": "Testing expiry behaviour of pending proposals.",
            "confidence": "medium",
        }
        result = validate_proposed_write(proposal, store_with_existing)
        assert result.confirm_id is not None

        _store._pending[result.confirm_id]["created_at"] = datetime.now(timezone.utc) - timedelta(
            minutes=15
        )

        confirm_result = confirm_write(result.confirm_id, store_with_existing)
        assert "error" in confirm_result


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


class TestCallerOperationPreservedAcrossT2Outcomes:
    """Ship-blocker 2 regression: caller-supplied `operation` must survive
    both Tier 2 outcomes (similarity found vs not), and the response must
    echo the actual operation that ran."""

    @pytest.fixture
    def store_with_seed(self, tmp_path: Path) -> Path:
        store_path = tmp_path / "projects" / "matrix"
        scaffold_project_store("matrix", store_path)
        append_decision(
            store_path,
            "Use Postgres as primary database",
            rationale="Mature ecosystem with strong JSON support and excellent tooling.",
            confidence="high",
            decision_type="data_model",
        )
        return store_path

    @pytest.mark.parametrize(
        "operation,similar_text,expected_status,expected_op",
        [
            ("add", False, "confirmed", "add"),
            ("add", True, "pending_confirmation", "add"),
            ("update", False, "confirmed", "update"),
            ("update", True, "pending_confirmation", "update"),
            ("supersede", False, "confirmed", "supersede"),
            ("supersede", True, "pending_confirmation", "supersede"),
        ],
    )
    def test_matrix(
        self,
        store_with_seed,
        operation,
        similar_text,
        expected_status,
        expected_op,
    ):
        if similar_text:
            title = "Switch to managed Postgres provider"
            rationale = "Migrate the existing Postgres workload to a managed instance."
        else:
            title = "Use Redis for write-through caching"
            rationale = "In-memory cache layer with pub/sub for invalidation events."

        affected = (
            "002-use-postgres-as-primary-database" if operation in ("update", "supersede") else None
        )

        result = validate_proposed_write(
            {"title": title, "rationale": rationale, "confidence": "high"},
            store_with_seed,
            operation=operation,
            affected_decision_id=affected,
        )
        assert result.status == expected_status
        assert result.operation == expected_op
