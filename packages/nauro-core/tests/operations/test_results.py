"""Kernel tests for the operations ``results`` module shell."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nauro_core.operations.results import RelatedDecision


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
