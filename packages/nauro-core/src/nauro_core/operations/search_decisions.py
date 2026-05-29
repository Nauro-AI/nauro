"""``search_decisions`` — BM25-rank decisions by query relevance.

All transports call this with the same arguments; each one wraps the
call to add transport-specific framing (``store`` field, telemetry
emission). The listing, BM25 ranking, and projection live here.

Status filtering happens here: by default only active decisions are
ranked. Pass ``include_superseded=True`` to also surface superseded
decisions (e.g. reviewing the prior rationale behind a current one).
Filtering before ranking keeps ``limit`` honored against the active set
rather than letting superseded hits crowd out active ones.
"""

from __future__ import annotations

from nauro_core.decision_model import Decision, DecisionStatus, parse_decision
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
    """Return BM25-ranked decisions for ``query``.

    Args:
        store: Storage adapter providing ``list_decisions`` / ``read_decision``.
        query: Search text. Empty or whitespace-only is rejected.
        limit: Maximum number of hits to return.
        include_superseded: When False (default), only active decisions are
            ranked. When True, superseded decisions are ranked as well.
        use_embeddings: When True, augment the BM25 result with the optional
            embedding retriever (union). Resolved by the adapter from
            env/config; the kernel stays I/O-free. Fail-open: if the optional
            dependency is absent the result is BM25-only.

    Returns:
        :class:`SearchDecisionsResult` with ``results`` populated on the
        success path (sorted by BM25 score descending, truncated to
        ``limit`` against the filtered set). On an empty/whitespace query,
        ``error`` is populated with ``kind="rejected"`` and ``results``
        stays empty.
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

    decisions = []
    for stem in store.list_decisions():
        body = store.read_decision(stem)
        if body is None:
            continue
        decisions.append(parse_decision(body, f"{stem}.md"))

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


def _append_embedding_hits(
    decisions: list[Decision],
    query: str,
    limit: int,
    bm25_hits: list[SearchHit],
) -> list[SearchHit]:
    """Append embedding top-k decisions BM25 did not surface (union pool).

    BM25 hits keep their order and shape; embedding-only hits are appended
    with ``score=0.0`` (no BM25 score) so the row stays serializable. The
    augmenter is fail-open: an absent dependency yields an empty pool and the
    BM25 hits pass through unchanged. The combined list is truncated to
    ``limit`` so the candidate pool stays bounded.
    """
    from nauro_core.embeddings import embedding_pool

    pool = embedding_pool(decisions, query, top_k=limit)
    if not pool:
        return bm25_hits

    seen = {hit.number for hit in bm25_hits}
    by_num = {d.num: d for d in decisions}
    augmented = list(bm25_hits)
    for num in pool:
        if num in seen:
            continue
        d = by_num.get(num)
        if d is None:
            continue
        seen.add(num)
        augmented.append(
            SearchHit(
                number=d.num,
                title=d.title,
                date=d.date.isoformat() if d.date else None,
                status=str(d.status.value),
                relevance_snippet=None,
                score=0.0,
            )
        )

    return augmented[:limit]
