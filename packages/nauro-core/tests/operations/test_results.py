"""Kernel tests for the operations ``results`` module shell."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nauro_core.operations.results import (
    DecisionSummary,
    ErrorPayload,
    GetDecisionResult,
    ListDecisionsResult,
    RelatedDecision,
    SearchDecisionsResult,
    SearchHit,
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
