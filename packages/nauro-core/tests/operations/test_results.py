"""Kernel tests for the operations ``results`` module shell."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nauro_core.operations.results import (
    DecisionSummary,
    DiffSinceLastSessionResult,
    ErrorPayload,
    GetContextResult,
    GetDecisionResult,
    ListDecisionsResult,
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
