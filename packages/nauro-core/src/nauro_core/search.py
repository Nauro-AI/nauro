"""BM25 search over decisions (D93).

Builds an in-memory BM25 index per call using bm25s + PyStemmer.
Index text: title + rationale for each decision.
"""

from __future__ import annotations

import re

import bm25s
import Stemmer

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.parsing import extract_relevance_snippet

_stemmer = Stemmer.Stemmer("english")


def bm25_search(
    decisions: list[Decision],
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Rank decisions by BM25 relevance to query.

    Returns list of result dicts sorted by BM25 score descending.
    Only results with score > 0 are included.
    """
    if not decisions or not query or not query.strip():
        return []

    corpus = [f"{d.title} {d.rationale}" for d in decisions]
    corpus_tokens = bm25s.tokenize(corpus, stopwords="en", stemmer=_stemmer)

    retriever = bm25s.BM25()
    retriever.index(corpus_tokens)

    k = min(limit, len(decisions))
    query_tokens = bm25s.tokenize([query], stopwords="en", stemmer=_stemmer)
    results, scores = retriever.retrieve(query_tokens, k=k)

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
            first_sentence = re.split(r"[.!?]\s", d.rationale, maxsplit=1)[0]
            snippet = first_sentence[:100].strip()
            if len(first_sentence) > 100:
                snippet += "..."

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
) -> list[dict]:
    """Retrieve top-k related active decisions for conflict checking.

    Returns list of dicts sorted by BM25 score descending.
    Only active decisions are considered; results with score <= 0 are excluded.

    ``stopwords`` is passed through to ``bm25s.tokenize`` and applies to both
    the corpus and query tokenization. Callers (e.g. tier-2 validation) may
    pass an extended list to filter domain-generic tokens that bm25s's
    default English list doesn't cover.
    """
    active = [d for d in decisions if d.status is DecisionStatus.active]
    if not active or not query_text or not query_text.strip():
        return []

    corpus = [f"{d.title} {d.rationale}" for d in active]
    corpus_tokens = bm25s.tokenize(corpus, stopwords=stopwords, stemmer=_stemmer)

    retriever = bm25s.BM25()
    retriever.index(corpus_tokens)

    k = min(top_k, len(active))
    query_tokens = bm25s.tokenize([query_text], stopwords=stopwords, stemmer=_stemmer)
    results, scores = retriever.retrieve(query_tokens, k=k)

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
