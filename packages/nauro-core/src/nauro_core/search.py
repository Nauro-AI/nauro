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

import bm25s
import Stemmer

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.parsing import extract_relevance_snippet, first_sentence_end

_stemmer = Stemmer.Stemmer("english")


def _index_text(d: Decision) -> str:
    """The BM25 corpus document for one decision.

    Title + rationale + rejected-alternative names (names only — see the
    module docstring for why reasons are excluded). Shared by
    :func:`bm25_search` and :func:`bm25_retrieve` so the two retrieval
    paths rank over the same corpus by construction.
    """
    names = " ".join(r.name for r in d.rejected)
    return f"{d.title} {d.rationale} {names}" if names else f"{d.title} {d.rationale}"


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
            # First sentence via the shared splitter, with the trailing
            # terminator dropped to match the prior snippet shape.
            end = first_sentence_end(d.rationale)
            first_sentence = d.rationale[:end].rstrip(".!?")
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
) -> list[dict]:
    """Retrieve a BM25 ∪ embedding candidate pool of related active decisions.

    With ``use_embeddings`` False this is exactly :func:`bm25_retrieve` — same
    arguments, same result list, byte-identical to the BM25-only path.

    With ``use_embeddings`` True the BM25 top-k results are returned first, in
    their existing order and shape, and any decision in the embedding top-k
    that BM25 did not already surface is appended as an embedding-sourced hit.
    Embedding-sourced hits carry ``similarity: None`` (the static-embedding
    cosine is not on the BM25 score scale, so it is not reported as a BM25
    score) and the same ``number`` / ``title`` / ``rationale_preview`` shape.

    The augmenter is fail-open: when the optional embedding dependency is
    absent or the model fails to load, the embedding pool is empty and the
    result is the BM25-only list. Importing the augmenter here keeps the
    optional dependency out of the module-level import graph.
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
