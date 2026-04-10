"""Tier 2 validation — BM25 similarity (D93).

Checks the proposal against existing decisions using BM25 ranking
via bm25s + PyStemmer. Replaces the previous embedding/Jaccard approach.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nauro_core.search import bm25_retrieve

from nauro.store.reader import _list_decisions

logger = logging.getLogger("nauro.validation.tier2")

TOP_K = 5


def check_similarity(proposal: dict, project_path: Path) -> tuple[str, list[dict]]:
    """Check proposal similarity against existing decisions using BM25.

    Returns:
        (action, similar_decisions) where action is "auto_confirm" or "needs_review".
    """
    decisions = _list_decisions(project_path)
    if not decisions:
        return ("auto_confirm", [])

    title = proposal.get("title", "")
    rationale = proposal.get("rationale", "")
    proposal_text = f"{title}. {rationale[:200]}"

    related = bm25_retrieve(decisions, proposal_text, top_k=TOP_K)
    if not related:
        return ("auto_confirm", [])

    similar = [
        {
            "id": f"decision-{r['number']:03d}",
            "title": r["title"],
            "similarity": r["similarity"],
            "rationale_preview": r["rationale_preview"],
        }
        for r in related
    ]
    return ("needs_review", similar)
