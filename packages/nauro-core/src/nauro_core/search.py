"""BM25 search over decisions.

Builds an in-memory BM25 index per call using bm25s + PyStemmer.
Index text per decision: title + rationale + rejected-alternative names.

Rejected names are indexed because they carry the vocabulary of paths the
project declined — the bridge for two conflict classes the title+rationale
text cannot reach: a cross-vocabulary supersession whose only shared token
lives in a rejected alternative's name, and a proposal that revisits an
explicitly-rejected path. Rejected reasons stay out of the index: they tie
on conflict catch but dilute ranking and inflate scores on verbose
unrelated queries (reason prose rewards verbosity; names carry the
declined path's identity).
"""

from __future__ import annotations

from typing import TypedDict

import bm25s
import Stemmer

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.parsing import _first_sentence_snippet, extract_relevance_snippet

_stemmer = Stemmer.Stemmer("english")


# Two distinct result shapes, deliberately not unified. Module-private (not in
# ``__all__``). These annotate the existing dict literals only; each dict is a
# plain dict at runtime, so the returned rows are unchanged.
class Bm25SearchRow(TypedDict):
    """One row of the ``bm25_search`` ranked result."""

    number: int
    title: str
    date: str | None
    status: str
    relevance_snippet: str
    score: float


class Bm25Hit(TypedDict):
    """One hit from ``bm25_retrieve`` / ``union_retrieve``."""

    number: int
    title: str
    similarity: float | None
    rationale_preview: str


def _index_text(d: Decision) -> str:
    """The BM25 corpus document for one decision: title, rationale, and
    rejected-alternative names only. Shared by :func:`bm25_search` and
    :func:`bm25_retrieve`, so both retrieval paths rank the same corpus.
    """
    names = " ".join(r.name for r in d.rejected)
    return f"{d.title} {d.rationale} {names}" if names else f"{d.title} {d.rationale}"


def bm25_search(
    decisions: list[Decision],
    query: str,
    limit: int = 10,
) -> list[Bm25SearchRow]:
    """Rank decisions by BM25 relevance to ``query``.

    Returns result dicts sorted by score descending, score > 0 only.
    """
    if not decisions or not query or not query.strip():
        return []

    corpus = [_index_text(d) for d in decisions]
    # show_progress=False — bm25s defaults to True; the tqdm output is invisible
    # in MCP server stderr but pollutes the `nauro check-decision` CLI surface.
    corpus_tokens = bm25s.tokenize(corpus, stopwords="en", stemmer=_stemmer, show_progress=False)

    retriever = bm25s.BM25()
    retriever.index(corpus_tokens, show_progress=False)

    # Clamp k into [0, N]: bm25s/numpy argpartition raises ValueError on a
    # negative k, which a negative limit would otherwise pass straight through.
    k = max(0, min(limit, len(decisions)))
    if k == 0:
        return []
    query_tokens = bm25s.tokenize([query], stopwords="en", stemmer=_stemmer, show_progress=False)
    results, scores = retriever.retrieve(query_tokens, k=k, show_progress=False)

    query_words = query.strip().split()
    ranked = []
    for i in range(results.shape[1]):
        idx = int(results[0, i])
        score = float(scores[0, i])
        if score <= 0:
            break

        d = decisions[idx]
        snippet = extract_relevance_snippet(d.rationale, query_words)
        if not snippet and d.rationale:
            snippet = _first_sentence_snippet(d.rationale)

        ranked.append(
            {
                "number": d.num,
                "title": d.title,
                "date": d.date.isoformat() if d.date else None,
                "status": str(d.status.value),
                "relevance_snippet": snippet,
                "score": round(score, 3),
            }
        )

    return ranked[:limit]


def bm25_retrieve(
    decisions: list[Decision],
    query_text: str,
    top_k: int = 5,
    stopwords: str | list[str] = "en",
) -> list[Bm25Hit]:
    """Retrieve top-k related active decisions for agent assessment, as dicts sorted
    by BM25 score descending. Only active decisions rank and score <= 0 is excluded.
    ``stopwords`` applies to both corpus and query tokenization.
    """
    active = [d for d in decisions if d.status is DecisionStatus.active]
    if not active or not query_text or not query_text.strip():
        return []

    corpus = [_index_text(d) for d in active]
    corpus_tokens = bm25s.tokenize(
        corpus, stopwords=stopwords, stemmer=_stemmer, show_progress=False
    )

    retriever = bm25s.BM25()
    retriever.index(corpus_tokens, show_progress=False)

    k = min(top_k, len(active))
    query_tokens = bm25s.tokenize(
        [query_text], stopwords=stopwords, stemmer=_stemmer, show_progress=False
    )
    results, scores = retriever.retrieve(query_tokens, k=k, show_progress=False)

    related = []
    for i in range(results.shape[1]):
        idx = int(results[0, i])
        score = float(scores[0, i])
        if score <= 0:
            break

        d = active[idx]
        related.append(
            {
                "number": d.num,
                "title": d.title,
                "similarity": round(score, 3),
                "rationale_preview": d.rationale[:200] if d.rationale else "",
            }
        )

    return related


def union_retrieve(
    decisions: list[Decision],
    query_text: str,
    top_k: int = 5,
    stopwords: str | list[str] = "en",
    use_embeddings: bool = False,
) -> list[Bm25Hit]:
    """Retrieve related active decisions as a BM25 union embedding candidate pool.
    ``use_embeddings`` False is exactly :func:`bm25_retrieve`; True keeps the BM25
    hits first, then appends embedding-only hits with ``similarity: None``, fail-open.
    """
    bm25_hits = bm25_retrieve(decisions, query_text, top_k=top_k, stopwords=stopwords)
    if not use_embeddings:
        return bm25_hits

    from nauro_core.embeddings import embedding_pool

    active = [d for d in decisions if d.status is DecisionStatus.active]
    if not active or not query_text or not query_text.strip():
        return bm25_hits

    pool = embedding_pool(active, query_text, top_k=top_k)
    if not pool:
        return bm25_hits

    seen = {hit["number"] for hit in bm25_hits}
    by_num = {d.num: d for d in active}
    augmented = list(bm25_hits)
    for num in pool:
        if num in seen:
            continue
        d = by_num.get(num)
        if d is None:
            continue
        seen.add(num)
        augmented.append(
            {
                "number": d.num,
                "title": d.title,
                "similarity": None,
                "rationale_preview": d.rationale[:200] if d.rationale else "",
            }
        )

    return augmented
