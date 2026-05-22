"""Kernel-level tests for ``operations.search_decisions`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` and asserts on the typed
:class:`SearchDecisionsResult` directly. Surface-level wiring tests live
in each transport's own suite.
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
    InMemoryStore,
    SearchDecisionsResult,
    search_decisions,
)


def _seed_decision(
    num: int,
    title: str,
    rationale: str = "Test rationale.",
    *,
    status: DecisionStatus = DecisionStatus.active,
    confidence: DecisionConfidence = DecisionConfidence.medium,
    decision_date: date | None = date(2026, 1, 1),
    stem: str | None = None,
) -> tuple[str, str]:
    """Return (file_stem, formatted_markdown) for a minimal v2 decision."""
    superseded_by = "999" if status is DecisionStatus.superseded else None
    decision = Decision(
        date=decision_date,
        confidence=confidence,
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
    result = search_decisions(InMemoryStore(), "anything")
    assert isinstance(result, SearchDecisionsResult)


def test_empty_store_returns_empty_results() -> None:
    result = search_decisions(InMemoryStore(), "anything")
    assert result.results == []
    assert result.error is None


def test_empty_query_returns_rejection_error() -> None:
    result = search_decisions(InMemoryStore(), "")
    assert result.results == []
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert "non-empty" in result.error.reason


def test_whitespace_query_returns_rejection_error() -> None:
    result = search_decisions(InMemoryStore(), "   ")
    assert result.results == []
    assert result.error is not None
    assert result.error.kind == "rejected"


def test_title_match_returns_hit_with_positive_score() -> None:
    stem, body = _seed_decision(
        1,
        "Use Auth0 for authentication",
        "Auth0 provides OAuth 2.1 support.",
    )
    store = _store_with((stem, body))
    result = search_decisions(store, "Auth0")
    assert len(result.results) == 1
    hit = result.results[0]
    assert "Auth0" in hit.title
    assert hit.score > 0


def test_rationale_match_populates_relevance_snippet() -> None:
    stem, body = _seed_decision(
        1,
        "Use FastAPI for MCP server",
        "FastAPI provides async support. Works well with Mangum for Lambda deployment.",
    )
    store = _store_with((stem, body))
    result = search_decisions(store, "Mangum")
    assert len(result.results) == 1
    hit = result.results[0]
    assert hit.relevance_snippet is not None
    assert "Mangum" in hit.relevance_snippet


def test_stemming_matches_morphological_variants() -> None:
    """BM25 stemming makes ``"authentication"`` match ``"authentication"`` in title."""
    stem, body = _seed_decision(
        1,
        "Use Auth0 for authentication",
        "Auth0 provides OAuth 2.1 support and handles JWT validation.",
    )
    store = _store_with((stem, body))
    # The query stem matches across the title and rationale; the test pins
    # that stemming-driven retrieval, not literal substring matching, is
    # the kernel's contract.
    result = search_decisions(store, "authenticating")
    assert len(result.results) == 1
    assert "Auth0" in result.results[0].title


def test_superseded_decisions_appear_in_results() -> None:
    """Status is not a filter on search; superseded rationale stays inspectable.

    Pins the no-status-filter contract for ``search_decisions`` so an agent
    looking at past doctrine can still surface a superseded decision by
    keyword. Status filtering belongs to ``list_decisions``.
    """
    active = _seed_decision(
        1,
        "Use Cognito for authentication",
        "Cognito ties tightly to AWS-native IAM.",
    )
    superseded = _seed_decision(
        2,
        "Use Auth0 for authentication",
        "Auth0 was the prior managed identity provider.",
        status=DecisionStatus.superseded,
    )
    store = _store_with(active, superseded)
    result = search_decisions(store, "Auth0")
    statuses = {hit.status for hit in result.results}
    assert "superseded" in statuses


def test_results_sorted_descending_by_score() -> None:
    """Hits come back sorted by BM25 score, descending.

    Pins the relevance-ordered contract for ``search_decisions``: result
    order follows the score column rather than decision number or
    insertion order.
    """
    seeded = [
        _seed_decision(
            1,
            "Use FastAPI",
            "FastAPI Lambda deployment notes.",
        ),
        _seed_decision(
            2,
            "Adopt Redis",
            "Redis for session cache only.",
        ),
        _seed_decision(
            3,
            "Use FastAPI for Lambda deployment",
            "FastAPI plus Mangum is the Lambda deployment combination.",
        ),
    ]
    store = _store_with(*seeded)
    result = search_decisions(store, "FastAPI Lambda deployment")
    assert len(result.results) >= 2
    scores = [hit.score for hit in result.results]
    assert scores == sorted(scores, reverse=True), (
        "search_decisions must return hits sorted by score descending"
    )


def test_limit_truncates_result_count() -> None:
    seeded = [
        _seed_decision(i, f"Search target {i}", f"Match phrase number {i} here.")
        for i in range(1, 6)
    ]
    store = _store_with(*seeded)
    result = search_decisions(store, "match phrase", limit=2)
    assert len(result.results) <= 2


def test_store_field_absent_from_result_model_dump() -> None:
    """Transports own the ``store`` field; the kernel never emits it."""
    result = search_decisions(InMemoryStore(), "x")
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped
