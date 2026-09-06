"""The cross-call BM25 index cache in ``search`` must be invisible to callers:
every call ranks exactly as a freshly built index would, across edits,
supersession, removal and restoration, and the two stopword settings never
serve each other's index.
"""

from __future__ import annotations

from conftest import make_decision

from nauro_core.decision_model import DecisionStatus
from nauro_core.search import _INDEX_BY_STOPWORDS, bm25_retrieve, bm25_search

EXTENDED = ["en", "decision", "approach"]


def _corpus() -> list:
    return [
        make_decision(1, "Use Auth0 for authentication", "Auth0 handles JWT validation."),
        make_decision(2, "Chose Memcached for session state", "Simpler than Redis for caching."),
        make_decision(3, "Use FastAPI for MCP server", "Async support and OpenAPI docs."),
        make_decision(
            4, "Defer multi-repo sync", "Single-repo scope for now.", status="superseded"
        ),
    ]


def _fresh_search(decisions, query, limit=10):
    _INDEX_BY_STOPWORDS.clear()
    return bm25_search(decisions, query, limit)


def _fresh_retrieve(decisions, query, stopwords="en"):
    _INDEX_BY_STOPWORDS.clear()
    return bm25_retrieve(decisions, query, top_k=5, stopwords=stopwords)


def test_unchanged_corpus_reuses_the_index() -> None:
    _INDEX_BY_STOPWORDS.clear()
    decisions = _corpus()
    bm25_search(decisions, "auth")
    first = _INDEX_BY_STOPWORDS["en"][1]
    bm25_search(decisions, "caching")
    assert _INDEX_BY_STOPWORDS["en"][1] is first


def test_every_corpus_change_ranks_like_a_fresh_index() -> None:
    queries = ["Auth0 JWT", "Redis caching", "FastAPI Lambda", "multi-repo sync", "nothing here"]
    decisions = _corpus()
    _INDEX_BY_STOPWORDS.clear()
    for query in queries:
        assert bm25_search(decisions, query) == _fresh_search(decisions, query)

    decisions[1] = decisions[1].model_copy(update={"rationale": "Now about Auth0 JWT validation."})
    for query in queries:
        assert bm25_search(decisions, query) == _fresh_search(decisions, query)

    decisions[0] = decisions[0].model_copy(
        update={"status": DecisionStatus.superseded, "superseded_by": "9"}
    )
    for query in queries:
        assert bm25_retrieve(decisions, query) == _fresh_retrieve(decisions, query)

    removed = decisions.pop(2)
    for query in queries:
        assert bm25_search(decisions, query) == _fresh_search(decisions, query)

    decisions.insert(2, removed)
    for query in queries:
        assert bm25_search(decisions, query) == _fresh_search(decisions, query)
        assert bm25_retrieve(decisions, query) == _fresh_retrieve(decisions, query)


def test_stopword_settings_keep_separate_indexes() -> None:
    _INDEX_BY_STOPWORDS.clear()
    decisions = _corpus()
    plain = bm25_retrieve(decisions, "decision approach Auth0", stopwords="en")
    extended = bm25_retrieve(decisions, "decision approach Auth0", stopwords=EXTENDED)
    assert set(_INDEX_BY_STOPWORDS) == {"en", tuple(EXTENDED)}
    assert plain == _fresh_retrieve(decisions, "decision approach Auth0", "en")
    assert extended == _fresh_retrieve(decisions, "decision approach Auth0", EXTENDED)


def test_search_and_active_only_retrieve_do_not_share_a_stale_corpus() -> None:
    _INDEX_BY_STOPWORDS.clear()
    decisions = _corpus()
    assert bm25_search(decisions, "multi-repo sync") == _fresh_search(decisions, "multi-repo sync")
    assert bm25_retrieve(decisions, "multi-repo sync") == _fresh_retrieve(
        decisions, "multi-repo sync"
    )
    assert bm25_retrieve(decisions, "multi-repo sync") == []
    assert bm25_search(decisions, "multi-repo sync")[0]["number"] == 4
