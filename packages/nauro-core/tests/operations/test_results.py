"""Kernel tests for the operations ``results`` module shell."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nauro_core.operations.results import ErrorPayload, GetDecisionResult, RelatedDecision


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
