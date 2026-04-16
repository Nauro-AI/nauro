"""Tier 2 validation — BM25 similarity (D93).

Thin adapter over ``nauro_core.validation.check_bm25_similarity``. The
filtering, stopword list, and retrieval live in nauro-core so the remote
MCP surface (mcp-server) gets the same outcome for the same proposal.
This module only contributes the local I/O (filesystem decision load) and
the ``id`` reshape that ``tier3`` needs for its lookup.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nauro_core.validation import check_bm25_similarity

from nauro.store.reader import _list_decisions

logger = logging.getLogger("nauro.validation.tier2")


def check_similarity(proposal: dict, project_path: Path) -> tuple[str, list[dict]]:
    """Check proposal similarity against existing decisions using BM25.

    Returns:
        (action, similar_decisions) where action is "auto_confirm" or "needs_review".
    """
    decisions = _list_decisions(project_path)
    action, related = check_bm25_similarity(proposal, decisions)
    if action == "auto_confirm":
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
