"""``search_decisions`` — BM25-rank decisions by query relevance.

All transports call this with the same arguments; each one wraps the
call to add transport-specific framing such as the ``store`` field. The
listing, BM25 ranking, and projection live here.

Status filtering happens here: by default only active decisions are
ranked. Pass ``include_superseded=True`` to also surface superseded
decisions (e.g. reviewing the prior rationale behind a current one).
Filtering before ranking keeps ``limit`` honored against the active set
rather than letting superseded hits crowd out active ones.
"""

from __future__ import annotations

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.operations.decision_lookup import parse_all_decisions
from nauro_core.operations.results import (
    ErrorPayload,
    SearchDecisionsResult,
    SearchHit,
)
from nauro_core.operations.store import Store
from nauro_core.search import bm25_search


def search_decisions(
    store: Store,
    query: str,
    limit: int = 10,
    include_superseded: bool = False,
    use_embeddings: bool = False,
) -> SearchDecisionsResult:
    """Return BM25-ranked decisions for ``query``, score descending, truncated to
    ``limit`` against the status-filtered set. ``include_superseded`` also ranks
    superseded rows; ``use_embeddings`` unions a pool, fail-open. Empty query rejects.
    """
    if not query or not query.strip():
        return SearchDecisionsResult(
            error=ErrorPayload(
                kind="rejected",
                reason=(
                    "search_decisions requires a non-empty query."
                    " Use list_decisions to browse all decisions."
                ),
            ),
        )

    if limit < 1:
        return SearchDecisionsResult(
            error=ErrorPayload(
                kind="rejected",
                reason="search_decisions requires a positive limit.",
            ),
        )

    decisions = parse_all_decisions(store)

    if not include_superseded:
        decisions = [d for d in decisions if d.status is DecisionStatus.active]

    ranked = bm25_search(decisions, query, limit=limit)
    hits = [
        SearchHit(
            number=row["number"],
            title=row["title"],
            date=row["date"],
            status=row["status"],
            # Coerce empty string to None so exclude_none=True strips the key on title-only hits.
            relevance_snippet=row["relevance_snippet"] or None,
            score=row["score"],
        )
        for row in ranked
    ]

    if use_embeddings:
        hits = _append_embedding_hits(decisions, query, limit, hits)

    return SearchDecisionsResult(results=hits)


# Slots reserved inside ``limit`` for embedding-only hits in search_decisions.
# search_decisions is a search tool: callers expect at most ``limit`` results,
# so the union cannot simply exceed the budget the way union_retrieve's
# fixed-top_k candidate pool does. Without a reservation, a healthy corpus
# fills ``limit`` with BM25 hits and every embedding-only hit lands past the
# cap and is sliced off — the augmenter would contribute nothing exactly when
# it should. Reserving a few slots guarantees the embedding pool widens the
# result (the augmenter's whole point) while the total stays bounded by
# ``limit``. The reserve is clamped to ``limit - 1`` so BM25 always retains at
# least one slot (and the majority at typical limits) as the primary signal.
_EMBEDDING_RESERVED_SLOTS = 3


def _append_embedding_hits(
    decisions: list[Decision],
    query: str,
    limit: int,
    bm25_hits: list[SearchHit],
) -> list[SearchHit]:
    """Blend embedding-only hits into the BM25 result under a hard ``limit``: BM25 hits
    keep order and shape, embedding-only decisions append with ``score=0.0`` into up
    to ``_EMBEDDING_RESERVED_SLOTS`` slots, trimming BM25 only as far as those fill.
    """
    from nauro_core.embeddings import embedding_pool

    pool = embedding_pool(decisions, query, top_k=limit)
    if not pool:
        return bm25_hits

    seen = {hit.number for hit in bm25_hits}
    by_num = {d.num: d for d in decisions}

    embedding_hits: list[SearchHit] = []
    # Clamp to ``limit - 1`` so BM25 keeps at least one slot whenever it has
    # hits. At ``limit == 1`` the reserve is 0 and the result is pure BM25 —
    # a single-result query returns the strongest lexical match, not an
    # embedding-only hit.
    reserve = min(_EMBEDDING_RESERVED_SLOTS, max(0, limit - 1))
    for num in pool:
        if len(embedding_hits) >= reserve:
            break
        if num in seen:
            continue
        d = by_num.get(num)
        if d is None:
            continue
        seen.add(num)
        embedding_hits.append(
            SearchHit(
                number=d.num,
                title=d.title,
                date=d.date.isoformat() if d.date else None,
                status=str(d.status.value),
                relevance_snippet=None,
                score=0.0,
            )
        )

    if not embedding_hits:
        return bm25_hits

    # Trim BM25 only enough to fit the embedding hits within ``limit``.
    bm25_budget = limit - len(embedding_hits)
    return bm25_hits[:bm25_budget] + embedding_hits
