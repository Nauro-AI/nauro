"""Kernel-level tests for ``operations.get_decision`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` and asserts on the typed
:class:`GetDecisionResult` directly. Surface-level wiring tests live in
each transport's own suite.
"""

from __future__ import annotations

from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import (
    GetDecisionResult,
    InMemoryStore,
    get_decision,
)


def _seed_decision(
    num: int,
    title: str,
    rationale: str,
    *,
    status: DecisionStatus = DecisionStatus.active,
    stem: str | None = None,
) -> tuple[str, str]:
    """Return (file_stem, formatted_markdown) for a minimal v2 decision."""
    superseded_by = "999" if status is DecisionStatus.superseded else None
    decision = Decision(
        date=date(2026, 1, 1),
        confidence=DecisionConfidence.medium,
        status=status,
        superseded_by=superseded_by,
        num=num,
        title=title,
        rationale=rationale,
    )
    if stem is None:
        slug = title.lower().replace(" ", "-")
        stem = f"{num:03d}-{slug}"
    return stem, format_decision(decision)


def _store_with(*decisions: tuple[str, str]) -> InMemoryStore:
    return InMemoryStore(decisions=dict(decisions))


def test_returns_result_type() -> None:
    result = get_decision(InMemoryStore(), 1)
    assert isinstance(result, GetDecisionResult)


def test_empty_store_returns_not_found_error() -> None:
    result = get_decision(InMemoryStore(), 1)
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert "1" in result.error.reason


def test_existing_decision_returns_full_content() -> None:
    stem, body = _seed_decision(7, "Adopt Redis", "Use Redis for session cache.")
    store = _store_with((stem, body))
    result = get_decision(store, 7)
    assert result.error is None
    assert result.content == body


def test_missing_number_returns_error_with_reason_naming_number() -> None:
    stem, body = _seed_decision(7, "Adopt Redis", "Use Redis for session cache.")
    store = _store_with((stem, body))
    result = get_decision(store, 42)
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert "42" in result.error.reason
    assert result.error.reason == "Decision 42 not found"


def test_number_resolves_from_stem_at_various_pad_widths() -> None:
    """``extract_decision_number`` accepts both unpadded and padded stems."""
    stem_5 = "5-short"
    stem_42 = "42-medium"
    stem_170 = "170-padded"
    _, body_5 = _seed_decision(5, "Short", "Test", stem=stem_5)
    _, body_42 = _seed_decision(42, "Medium", "Test", stem=stem_42)
    _, body_170 = _seed_decision(170, "Padded", "Test", stem=stem_170)
    store = _store_with((stem_5, body_5), (stem_42, body_42), (stem_170, body_170))

    for number, expected_body in ((5, body_5), (42, body_42), (170, body_170)):
        result = get_decision(store, number)
        assert result.error is None, f"unexpected miss for D{number}"
        assert result.content == expected_body


def test_superseded_decisions_still_resolve() -> None:
    """Status filtering belongs to ``list_decisions``, not ``get_decision``.

    A caller asking for a specific number must always receive the body
    when it exists — even if the decision was later superseded — so the
    rationale stays inspectable.
    """
    stem, body = _seed_decision(
        9,
        "Adopt REST endpoints",
        "Initial transport choice, later replaced by gRPC.",
        status=DecisionStatus.superseded,
    )
    store = _store_with((stem, body))
    result = get_decision(store, 9)
    assert result.error is None
    assert result.content == body


def test_store_field_absent_from_result_model_dump() -> None:
    """Transports own the ``store`` field; the kernel never emits it."""
    result = get_decision(InMemoryStore(), 1)
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped
