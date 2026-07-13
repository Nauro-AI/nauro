"""Kernel-level tests for ``operations.search_decisions`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` and asserts on the typed
:class:`SearchDecisionsResult` directly. Surface-level wiring tests live
in each transport's own suite.
"""

from __future__ import annotations

from conftest import _seed_decision, _store_with

from nauro_core.decision_model import DecisionStatus
from nauro_core.operations import (
    InMemoryStore,
    SearchDecisionsResult,
    search_decisions,
)


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


def test_non_positive_limit_returns_rejection_error() -> None:
    result = search_decisions(InMemoryStore(), "anything", limit=-1)
    assert result.results == []
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert "limit" in result.error.reason


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


def test_superseded_excluded_by_default() -> None:
    """search_decisions filters status in the kernel: superseded decisions are
    excluded by default so they cannot crowd active hits out of ``limit``.
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
    result = search_decisions(store, "authentication")
    statuses = {hit.status for hit in result.results}
    assert "superseded" not in statuses
    assert "active" in statuses


def test_include_superseded_true_retains_superseded() -> None:
    """Passing ``include_superseded=True`` surfaces superseded decisions."""
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
    result = search_decisions(store, "Auth0", include_superseded=True)
    statuses = {hit.status for hit in result.results}
    assert "superseded" in statuses


def test_filter_runs_before_truncation() -> None:
    """Status filtering runs before the ``limit`` truncation, so an active-only
    query returns active hits even when superseded decisions would otherwise
    outrank them in the BM25 top-N.
    """
    superseded = [
        _seed_decision(
            i,
            f"Superseded match {i}",
            "match phrase superseded here.",
            status=DecisionStatus.superseded,
        )
        for i in range(1, 4)
    ]
    active = [
        _seed_decision(i, f"Active match {i}", "match phrase active here.") for i in range(4, 7)
    ]
    store = _store_with(*superseded, *active)
    result = search_decisions(store, "match phrase", limit=3)
    statuses = {hit.status for hit in result.results}
    assert result.results, "active decisions matching the query should be returned"
    assert "superseded" not in statuses
    assert len(result.results) <= 3


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
