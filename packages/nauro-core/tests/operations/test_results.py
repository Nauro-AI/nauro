"""Kernel tests for the operations ``results`` module shell."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nauro_core.operations.results import (
    DecisionSummary,
    DiffSinceLastSessionResult,
    ErrorPayload,
    FlagQuestionResult,
    GetContextResult,
    GetDecisionResult,
    ListDecisionsResult,
    ProposeDecisionResult,
    RelatedDecision,
    SearchDecisionsResult,
    SearchHit,
    UpdateStateResult,
)


def test_related_decision_model_dump_shape() -> None:
    hit = RelatedDecision(
        id="decision-042",
        title="Use Postgres",
        score=1.5,
        status="active",
        date="2026-01-01",
        rationale_preview="ACID compliance trumps document flexibility for this workload.",
    )
    dumped = hit.model_dump(mode="json")
    assert dumped == {
        "id": "decision-042",
        "title": "Use Postgres",
        "score": 1.5,
        "status": "active",
        "date": "2026-01-01",
        "rationale_preview": "ACID compliance trumps document flexibility for this workload.",
    }


def test_related_decision_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RelatedDecision(
            id="decision-042",
            title="Use Postgres",
            score=1.5,
            status="active",
            date="2026-01-01",
            rationale_preview="x",
            unexpected_field="value",
        )


def test_related_decision_is_frozen() -> None:
    hit = RelatedDecision(
        id="decision-042",
        title="Use Postgres",
        score=1.5,
        status="active",
        date="2026-01-01",
        rationale_preview="x",
    )
    with pytest.raises(ValidationError):
        hit.title = "Reassigned"


def test_get_decision_result_success_with_content() -> None:
    result = GetDecisionResult(content="# Decision body\n")
    assert result.content == "# Decision body\n"
    assert result.error is None


def test_get_decision_result_error_with_payload() -> None:
    result = GetDecisionResult(error=ErrorPayload(kind="error", reason="Decision 42 not found"))
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert result.error.reason == "Decision 42 not found"


def test_get_decision_result_exclude_none_strips_empties() -> None:
    success = GetDecisionResult(content="body")
    assert success.model_dump(mode="json", exclude_none=True) == {"content": "body"}

    miss = GetDecisionResult(error=ErrorPayload(kind="error", reason="Decision 1 not found"))
    assert miss.model_dump(mode="json", exclude_none=True) == {
        "error": {"kind": "error", "reason": "Decision 1 not found"},
    }


def test_get_decision_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        GetDecisionResult(content="body", unexpected_field="value")


def test_get_decision_result_is_frozen() -> None:
    result = GetDecisionResult(content="body")
    with pytest.raises(ValidationError):
        result.content = "reassigned"


def test_decision_summary_full_row_shape() -> None:
    row = DecisionSummary(
        number=42,
        title="Adopt PostgreSQL",
        date="2026-01-01",
        status="active",
        type="infrastructure",
        confidence="high",
    )
    assert row.model_dump(mode="json") == {
        "number": 42,
        "title": "Adopt PostgreSQL",
        "date": "2026-01-01",
        "status": "active",
        "type": "infrastructure",
        "confidence": "high",
    }


def test_decision_summary_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DecisionSummary(
            number=1,
            title="x",
            status="active",
            confidence="medium",
            unexpected_field="value",
        )


def test_decision_summary_is_frozen() -> None:
    row = DecisionSummary(number=1, title="x", status="active", confidence="medium")
    with pytest.raises(ValidationError):
        row.title = "reassigned"


def test_decision_summary_exclude_none_strips_unset_optional_fields() -> None:
    row = DecisionSummary(number=1, title="x", status="active", confidence="medium")
    assert row.model_dump(mode="json", exclude_none=True) == {
        "number": 1,
        "title": "x",
        "status": "active",
        "confidence": "medium",
    }


def test_list_decisions_result_default_empty() -> None:
    result = ListDecisionsResult()
    assert result.decisions == []
    assert result.model_dump(mode="json", exclude_none=True) == {"decisions": []}


def test_list_decisions_result_holds_summaries() -> None:
    row = DecisionSummary(number=1, title="x", status="active", confidence="medium")
    result = ListDecisionsResult(decisions=[row])
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "decisions": [
            {
                "number": 1,
                "title": "x",
                "status": "active",
                "confidence": "medium",
            }
        ]
    }


def test_list_decisions_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ListDecisionsResult(decisions=[], unexpected_field="value")


def test_list_decisions_result_is_frozen() -> None:
    result = ListDecisionsResult()
    with pytest.raises(ValidationError):
        result.decisions = [
            DecisionSummary(number=1, title="x", status="active", confidence="medium")
        ]


def test_search_hit_full_row_shape() -> None:
    hit = SearchHit(
        number=42,
        title="Adopt PostgreSQL",
        date="2026-01-01",
        status="active",
        relevance_snippet="ACID semantics matter here.",
        score=1.875,
    )
    assert hit.model_dump(mode="json") == {
        "number": 42,
        "title": "Adopt PostgreSQL",
        "date": "2026-01-01",
        "status": "active",
        "relevance_snippet": "ACID semantics matter here.",
        "score": 1.875,
    }


def test_search_hit_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SearchHit(
            number=1,
            title="x",
            status="active",
            score=0.5,
            unexpected_field="value",
        )


def test_search_hit_is_frozen() -> None:
    hit = SearchHit(number=1, title="x", status="active", score=0.5)
    with pytest.raises(ValidationError):
        hit.title = "reassigned"


def test_search_hit_exclude_none_strips_unset_snippet() -> None:
    hit = SearchHit(number=1, title="x", status="active", score=0.5)
    assert hit.model_dump(mode="json", exclude_none=True) == {
        "number": 1,
        "title": "x",
        "status": "active",
        "score": 0.5,
    }


def test_search_decisions_result_default_empty() -> None:
    result = SearchDecisionsResult()
    assert result.results == []
    assert result.error is None
    assert result.model_dump(mode="json", exclude_none=True) == {"results": []}


def test_search_decisions_result_holds_hits() -> None:
    hit = SearchHit(
        number=1,
        title="x",
        date="2026-01-01",
        status="active",
        relevance_snippet="snippet",
        score=1.0,
    )
    result = SearchDecisionsResult(results=[hit])
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "results": [
            {
                "number": 1,
                "title": "x",
                "date": "2026-01-01",
                "status": "active",
                "relevance_snippet": "snippet",
                "score": 1.0,
            }
        ]
    }


def test_search_decisions_result_error_payload() -> None:
    result = SearchDecisionsResult(
        error=ErrorPayload(kind="rejected", reason="empty query"),
    )
    assert result.results == []
    assert result.model_dump(mode="json", exclude_none=True) == {
        "results": [],
        "error": {"kind": "rejected", "reason": "empty query"},
    }


def test_search_decisions_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SearchDecisionsResult(results=[], unexpected_field="value")


def test_search_decisions_result_is_frozen() -> None:
    result = SearchDecisionsResult()
    with pytest.raises(ValidationError):
        result.results = [SearchHit(number=1, title="x", status="active", score=0.5)]


def test_get_context_result_success_with_content() -> None:
    result = GetContextResult(content="# Current State\n\nShipping v1.\n")
    assert result.content == "# Current State\n\nShipping v1.\n"
    assert result.error is None


def test_get_context_result_error_with_payload() -> None:
    result = GetContextResult(error=ErrorPayload(kind="rejected", reason="Invalid level: 7"))
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert result.error.reason == "Invalid level: 7"


def test_get_context_result_exclude_none_strips_empties() -> None:
    success = GetContextResult(content="body")
    assert success.model_dump(mode="json", exclude_none=True) == {"content": "body"}

    miss = GetContextResult(error=ErrorPayload(kind="rejected", reason="Invalid level: 9"))
    assert miss.model_dump(mode="json", exclude_none=True) == {
        "error": {"kind": "rejected", "reason": "Invalid level: 9"},
    }


def test_get_context_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        GetContextResult(content="body", unexpected_field="value")


def test_get_context_result_is_frozen() -> None:
    result = GetContextResult(content="body")
    with pytest.raises(ValidationError):
        result.content = "reassigned"


def test_diff_since_last_session_result_success_with_diff() -> None:
    result = DiffSinceLastSessionResult(diff="Changes from v001 → v002\n")
    assert result.diff == "Changes from v001 → v002\n"
    assert result.cutoff_date_used is None
    assert result.error is None


def test_diff_since_last_session_result_cutoff_date_used_round_trips() -> None:
    result = DiffSinceLastSessionResult(
        diff="No snapshots available.",
        cutoff_date_used="2026-04-24T10:00:00+00:00",
    )
    assert result.cutoff_date_used == "2026-04-24T10:00:00+00:00"


def test_diff_since_last_session_result_exclude_none_strips_empties() -> None:
    success = DiffSinceLastSessionResult(diff="body")
    assert success.model_dump(mode="json", exclude_none=True) == {"diff": "body"}

    with_cutoff = DiffSinceLastSessionResult(
        diff="No snapshots available.",
        cutoff_date_used="2026-04-24T10:00:00+00:00",
    )
    assert with_cutoff.model_dump(mode="json", exclude_none=True) == {
        "diff": "No snapshots available.",
        "cutoff_date_used": "2026-04-24T10:00:00+00:00",
    }


def test_diff_since_last_session_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DiffSinceLastSessionResult(diff="body", unexpected_field="value")


def test_diff_since_last_session_result_is_frozen() -> None:
    result = DiffSinceLastSessionResult(diff="body")
    with pytest.raises(ValidationError):
        result.diff = "reassigned"


def test_update_state_result_default_status_ok() -> None:
    result = UpdateStateResult()
    assert result.status == "ok"
    assert result.warning is None
    assert result.error is None


def test_update_state_result_noop_status() -> None:
    result = UpdateStateResult(status="noop")
    assert result.status == "noop"
    assert result.warning is None


def test_update_state_result_with_warning() -> None:
    result = UpdateStateResult(status="ok", warning="overlap caution")
    assert result.warning == "overlap caution"


def test_update_state_result_exclude_none_strips_empties() -> None:
    noop = UpdateStateResult(status="noop")
    assert noop.model_dump(mode="json", exclude_none=True) == {"status": "noop"}

    plain = UpdateStateResult(status="ok")
    assert plain.model_dump(mode="json", exclude_none=True) == {"status": "ok"}

    with_warning = UpdateStateResult(status="ok", warning="careful")
    assert with_warning.model_dump(mode="json", exclude_none=True) == {
        "status": "ok",
        "warning": "careful",
    }


def test_update_state_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        UpdateStateResult(status="ok", unexpected_field="value")


def test_update_state_result_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        UpdateStateResult(status="weird")


def test_update_state_result_is_frozen() -> None:
    result = UpdateStateResult(status="ok")
    with pytest.raises(ValidationError):
        result.status = "noop"


def test_flag_question_result_default_status_ok() -> None:
    result = FlagQuestionResult()
    assert result.status == "ok"
    assert result.num is None
    assert result.error is None


def test_flag_question_result_with_num() -> None:
    result = FlagQuestionResult(status="ok", num=42)
    assert result.num == 42


def test_flag_question_result_exclude_none_strips_empties() -> None:
    bare = FlagQuestionResult()
    assert bare.model_dump(mode="json", exclude_none=True) == {"status": "ok"}

    with_num = FlagQuestionResult(num=7)
    assert with_num.model_dump(mode="json", exclude_none=True) == {
        "status": "ok",
        "num": 7,
    }


def test_flag_question_result_accepts_relocation_fields() -> None:
    result = FlagQuestionResult(
        status="ok",
        relocated_ids=("Q1", "Q9"),
        skipped_prose_ids=("Q7",),
    )
    assert result.relocated_ids == ("Q1", "Q9")
    assert result.skipped_prose_ids == ("Q7",)


def test_flag_question_result_relocation_fields_default_none_dropped_by_exclude_none() -> None:
    bare = FlagQuestionResult()
    assert bare.relocated_ids is None
    assert bare.skipped_prose_ids is None
    dumped = bare.model_dump(mode="json", exclude_none=True)
    assert "relocated_ids" not in dumped
    assert "skipped_prose_ids" not in dumped

    with_relocation = FlagQuestionResult(status="ok", relocated_ids=("Q1",))
    assert with_relocation.model_dump(mode="json", exclude_none=True) == {
        "status": "ok",
        "relocated_ids": ["Q1"],
    }


def test_flag_question_result_relocation_fields_are_frozen() -> None:
    result = FlagQuestionResult(status="ok", relocated_ids=("Q1",))
    with pytest.raises(ValidationError):
        result.relocated_ids = ("Q2",)


def test_flag_question_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FlagQuestionResult(status="ok", num=1, unexpected_field="value")


def test_flag_question_result_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        FlagQuestionResult(status="noop", num=1)


def test_flag_question_result_is_frozen() -> None:
    result = FlagQuestionResult(num=1)
    with pytest.raises(ValidationError):
        result.num = 2


def test_propose_decision_result_required_fields() -> None:
    """``status``, ``tier``, and ``operation`` are required; other fields
    are optional with sensible defaults."""
    result = ProposeDecisionResult(status="confirmed", tier=2, operation="add")
    assert result.status == "confirmed"
    assert result.tier == 2
    assert result.operation == "add"
    assert result.similar_decisions == []
    assert result.assessment == ""
    assert result.decision_id is None
    assert result.touched_decisions == []
    assert result.resolved_questions == []
    assert result.error is None


def test_propose_decision_result_confirmed_envelope() -> None:
    result = ProposeDecisionResult(
        status="confirmed",
        tier=2,
        operation="add",
        decision_id="042-adopt-postgres",
        touched_decisions=["042-adopt-postgres"],
        assessment="No similar existing decisions found.",
    )
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "status": "confirmed",
        "tier": 2,
        "operation": "add",
        "similar_decisions": [],
        "assessment": "No similar existing decisions found.",
        "decision_id": "042-adopt-postgres",
        "touched_decisions": ["042-adopt-postgres"],
        "resolved_questions": [],
    }


def test_propose_decision_result_confirmed_envelope_with_advisory_similars() -> None:
    """Tier 2 hits ride along on the confirmed envelope as advisory
    context; the write committed on the same call."""
    related = RelatedDecision(
        id="decision-042",
        title="Adopt PostgreSQL",
        score=0.5,
        status="active",
        date="2026-01-01",
        rationale_preview="x",
    )
    result = ProposeDecisionResult(
        status="confirmed",
        tier=2,
        operation="add",
        decision_id="050-adopt-postgres-readreplicas",
        touched_decisions=["050-adopt-postgres-readreplicas"],
        similar_decisions=[related],
        assessment="Tier 2 surfaced similar decisions; review them before further writes.",
    )
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped["status"] == "confirmed"
    assert dumped["decision_id"] == "050-adopt-postgres-readreplicas"
    assert len(dumped["similar_decisions"]) == 1
    assert dumped["similar_decisions"][0]["id"] == "decision-042"


def test_propose_decision_result_rejected_envelope() -> None:
    result = ProposeDecisionResult(
        status="rejected",
        tier=1,
        operation="reject",
        assessment="Title is empty.",
    )
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped["status"] == "rejected"
    assert dumped["tier"] == 1
    assert dumped["operation"] == "reject"
    assert dumped["assessment"] == "Title is empty."
    assert "decision_id" not in dumped
    assert "error" not in dumped


def test_propose_decision_result_rejected_with_error_payload() -> None:
    result = ProposeDecisionResult(
        status="rejected",
        tier=2,
        operation="reject",
        assessment="supersede half-state.",
        error=ErrorPayload(kind="error", reason="old decision not flipped"),
        touched_decisions=["003-new-decision"],
    )
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped["error"] == {"kind": "error", "reason": "old decision not flipped"}
    assert dumped["touched_decisions"] == ["003-new-decision"]


def test_propose_decision_result_accepts_relocation_fields() -> None:
    result = ProposeDecisionResult(
        status="confirmed",
        tier=2,
        operation="add",
        relocated_ids=("Q1",),
        skipped_prose_ids=("Q7",),
    )
    assert result.relocated_ids == ("Q1",)
    assert result.skipped_prose_ids == ("Q7",)


def test_propose_decision_result_relocation_fields_default_none_dropped_by_exclude_none() -> None:
    bare = ProposeDecisionResult(status="confirmed", tier=2, operation="add")
    assert bare.relocated_ids is None
    assert bare.skipped_prose_ids is None
    dumped = bare.model_dump(mode="json", exclude_none=True)
    assert "relocated_ids" not in dumped
    assert "skipped_prose_ids" not in dumped

    with_relocation = ProposeDecisionResult(
        status="confirmed", tier=2, operation="add", relocated_ids=("Q1",)
    )
    assert with_relocation.model_dump(mode="json", exclude_none=True)["relocated_ids"] == ["Q1"]


def test_propose_decision_result_relocation_fields_are_frozen() -> None:
    result = ProposeDecisionResult(
        status="confirmed", tier=2, operation="add", relocated_ids=("Q1",)
    )
    with pytest.raises(ValidationError):
        result.relocated_ids = ("Q2",)


def test_propose_decision_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProposeDecisionResult(
            status="confirmed",
            tier=2,
            operation="add",
            unexpected_field="value",
        )


def test_propose_decision_result_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        ProposeDecisionResult(status="weird", tier=2, operation="add")


def test_propose_decision_result_rejects_invalid_operation() -> None:
    with pytest.raises(ValidationError):
        ProposeDecisionResult(status="confirmed", tier=2, operation="invalid")


def test_propose_decision_result_is_frozen() -> None:
    result = ProposeDecisionResult(status="confirmed", tier=2, operation="add")
    with pytest.raises(ValidationError):
        result.status = "rejected"
