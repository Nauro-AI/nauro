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

        # D133: update appends rationale only — title="" + no metadata so the
        # disallowed-fields branch does not fire. add/supersede send the full
        # proposal as before.
        if operation == "update":
            proposal = {"title": "", "rationale": rationale}
        else:
            proposal = {"title": title, "rationale": rationale, "confidence": "high"}

        result = validate_proposed_write(
            proposal,
            store_with_seed,
            operation=operation,
            affected_decision_id=affected,
        )
        assert result.status == expected_status
        assert result.operation == expected_op


class TestD133UpdateRejectsMetadata:
    """D134 (mirroring D133): operation='update' rejects metadata fields at the
    local boundary so the canonical wording in PROPOSE_DECISION_OPERATIONS
    holds on both transports.
    """

    @pytest.fixture
    def store_with_seed(self, tmp_path: Path) -> Path:
        store_path = tmp_path / "projects" / "d133"
        scaffold_project_store("d133", store_path)
        append_decision(
            store_path,
            "Seed decision for D133 tests",
            rationale="A seed decision the update tests can target as affected_decision_id.",
        )
        return store_path

    RATIONALE = (
        "A sufficiently long rationale that comfortably exceeds the structural "
        "minimum length so D133 rejection wins on the disallowed-fields branch."
    )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", "A new title"),
            ("rejected", [{"alternative": "x", "reason": "y"}]),
            ("files_affected", ["foo.py"]),
            ("decision_type", "architecture"),
            ("reversibility", "easy"),
            ("confidence", "high"),
        ],
    )
    def test_update_with_single_metadata_field_is_rejected(
        self, store_with_seed: Path, field: str, value: object
    ) -> None:
        proposal = {"title": "", "rationale": self.RATIONALE, field: value}
        result = validate_proposed_write(
            proposal,
            store_with_seed,
            operation="update",
            affected_decision_id="002-seed-decision-for-d133-tests",
        )
        assert result.status == "rejected"
        assert result.tier == 0
        assert field in result.assessment
        assert 'operation="supersede"' in result.assessment

    def test_update_with_multiple_metadata_fields_lists_all(self, store_with_seed: Path) -> None:
        proposal = {
            "title": "Changed title",
            "rationale": self.RATIONALE,
            "confidence": "high",
            "decision_type": "architecture",
        }
        result = validate_proposed_write(
            proposal,
            store_with_seed,
            operation="update",
            affected_decision_id="002-seed-decision-for-d133-tests",
        )
        assert result.status == "rejected"
        assert result.tier == 0
        for field in ("title", "decision_type", "confidence"):
            assert field in result.assessment

    def test_update_with_empty_title_and_no_metadata_passes_d133_gate(
        self, store_with_seed: Path
    ) -> None:
        """The legitimate rationale-only update signal: title="" and none of the
        disallowed fields populated. Validation should advance past the D133
        gate (status may be confirmed or pending_confirmation depending on
        Tier 2 — either way, the disallowed-fields rejection must not fire)."""
        proposal = {"title": "", "rationale": self.RATIONALE}
        result = validate_proposed_write(
            proposal,
            store_with_seed,
            operation="update",
            affected_decision_id="002-seed-decision-for-d133-tests",
        )
        assert result.status != "rejected"
        assert result.tier != 0

    def test_update_with_empty_rationale_rejects_at_tier_1_not_d133(
        self, store_with_seed: Path
    ) -> None:
        """Update with empty rationale should fail on the rationale-only Tier 1
        check, not on the D133 disallowed-fields branch (which only fires for
        metadata fields, not for missing rationale)."""
        proposal = {"title": "", "rationale": ""}
        result = validate_proposed_write(
            proposal,
            store_with_seed,
            operation="update",
            affected_decision_id="002-seed-decision-for-d133-tests",
        )
        assert result.status == "rejected"
        assert result.tier == 1
        assert "Rationale is empty" in result.assessment

    def test_update_with_short_rationale_rejects_at_tier_1(self, store_with_seed: Path) -> None:
        proposal = {"title": "", "rationale": "too short"}
        result = validate_proposed_write(
            proposal,
            store_with_seed,
            operation="update",
            affected_decision_id="002-seed-decision-for-d133-tests",
        )
        assert result.status == "rejected"
        assert result.tier == 1
        assert "Rationale too short" in result.assessment

    def test_add_and_supersede_unaffected_by_d133_branch(self, store_with_seed: Path) -> None:
        """The disallowed-fields branch only fires for operation='update'."""
        proposal = {
            "title": "A new architectural decision",
            "rationale": self.RATIONALE,
            "confidence": "high",
            "decision_type": "architecture",
        }
        result_add = validate_proposed_write(
            proposal,
            store_with_seed,
            operation="add",
        )
        assert result_add.status != "rejected" or result_add.tier != 0
        result_supersede = validate_proposed_write(
            proposal,
            store_with_seed,
            operation="supersede",
            affected_decision_id="002-seed-decision-for-d133-tests",
        )
        assert result_supersede.status != "rejected" or result_supersede.tier != 0
