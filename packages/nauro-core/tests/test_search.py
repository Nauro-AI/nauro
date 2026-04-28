"""Tests for BM25 search (D93)."""

from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
)
from nauro_core.search import bm25_retrieve, bm25_search


def _make_decision(num: int, title: str, rationale: str, status: str = "active") -> Decision:
    status_enum = DecisionStatus(status)
    return Decision(
        date=date(2026, 4, 7),
        confidence=DecisionConfidence.medium,
        status=status_enum,
        superseded_by="999" if status_enum is DecisionStatus.superseded else None,
        num=num,
        title=title,
        rationale=rationale,
    )


DECISIONS = [
    _make_decision(
        1,
        "Use Auth0 for authentication",
        "Auth0 provides OAuth 2.1 support and handles JWT validation.",
    ),
    _make_decision(
        2,
        "Chose Memcached for session state",
        "Memcached is simpler than Redis for session caching. "
        "Lower operational overhead and sufficient for our read-heavy workload.",
    ),
    _make_decision(
        3,
        "Use FastAPI for MCP server",
        "FastAPI provides async support and automatic OpenAPI docs. "
        "Works well with Mangum for Lambda deployment.",
    ),
    _make_decision(
        4,
        "Defer multi-repo sync to v2",
        "Current scope is single-repo. Multi-repo sync adds complexity "
        "that is not justified at launch.",
        status="superseded",
    ),
]


class TestBm25Search:
    def test_basic_match(self):
        results = bm25_search(DECISIONS, "authentication")
        assert len(results) >= 1
        assert results[0]["title"] == "Use Auth0 for authentication"

    def test_stemming_matches_vocabulary_variants(self):
        """'deploying' should match 'deployment' via stemming."""
        results = bm25_search(DECISIONS, "deploying Lambda")
        assert any("FastAPI" in r["title"] for r in results)

    def test_vocabulary_mismatch_case(self):
        """The Redis/Memcached case from D93 motivation."""
        results = bm25_search(DECISIONS, "Use Redis for session caching")
        assert any("Memcached" in r["title"] for r in results)

    def test_no_match_returns_empty(self):
        results = bm25_search(DECISIONS, "quantum computing blockchain")
        assert results == []

    def test_empty_corpus(self):
        assert bm25_search([], "test") == []

    def test_empty_query(self):
        assert bm25_search(DECISIONS, "") == []
        assert bm25_search(DECISIONS, "   ") == []

    def test_limit(self):
        results = bm25_search(DECISIONS, "session authentication server", limit=2)
        assert len(results) <= 2

    def test_score_ordering(self):
        results = bm25_search(DECISIONS, "session caching Memcached")
        if len(results) >= 2:
            assert results[0]["score"] >= results[1]["score"]

    def test_includes_superseded(self):
        results = bm25_search(DECISIONS, "multi-repo sync")
        assert any(r["status"] == "superseded" for r in results)

    def test_result_has_snippet(self):
        results = bm25_search(DECISIONS, "Mangum")
        assert len(results) >= 1
        assert results[0]["relevance_snippet"]


class TestBm25Retrieve:
    def test_returns_related(self):
        related = bm25_retrieve(DECISIONS, "authentication OAuth provider")
        assert len(related) >= 1
        assert related[0]["title"] == "Use Auth0 for authentication"

    def test_active_only(self):
        """Superseded decisions are excluded from retrieval."""
        related = bm25_retrieve(DECISIONS, "multi-repo sync defer")
        numbers = [r["number"] for r in related]
        assert 4 not in numbers

    def test_rationale_preview_populated(self):
        related = bm25_retrieve(DECISIONS, "session caching")
        assert len(related) >= 1
        assert related[0]["rationale_preview"]

    def test_empty_decisions(self):
        assert bm25_retrieve([], "test") == []

    def test_empty_query(self):
        assert bm25_retrieve(DECISIONS, "") == []

    def test_top_k(self):
        related = bm25_retrieve(DECISIONS, "server session authentication", top_k=1)
        assert len(related) <= 1
