"""``search_decisions`` — BM25-rank decisions by query relevance.

All transports call this with the same arguments; each one wraps the
call to add transport-specific framing (``store`` field, telemetry
emission). The listing, BM25 ranking, and projection live here.

Superseded decisions stay in the result set: an agent searching the
project history may legitimately want to surface the prior rationale
behind a current decision. Status filtering belongs to
``list_decisions``.
"""

from __future__ import annotations

from nauro_core.decision_model import parse_decision
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
) -> SearchDecisionsResult:
    """Return BM25-ranked decisions for ``query``.

    Args:
        store: Storage adapter providing ``list_decisions`` / ``read_decision``.
        query: Search text. Empty or whitespace-only is rejected.
        limit: Maximum number of hits to return.

    Returns:
        :class:`SearchDecisionsResult` with ``results`` populated on the
        success path (sorted by BM25 score descending, truncated to
        ``limit``). On an empty/whitespace query, ``error`` is populated
        with ``kind="rejected"`` and ``results`` stays empty.
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
    return SearchDecisionsResult(results=hits)
