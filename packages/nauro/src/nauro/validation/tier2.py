"""Tier 2 validation — BM25 similarity (D93).

Checks the proposal against existing decisions using BM25 ranking
via bm25s + PyStemmer. Replaces the previous embedding/Jaccard approach.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bm25s.stopwords import STOPWORDS_EN
from nauro_core.search import bm25_retrieve

from nauro.store.reader import _list_decisions

logger = logging.getLogger("nauro.validation.tier2")

TOP_K = 5

# bm25s's default English stopword list is minimal (~30 tokens: a, an, and,
# are, as, at, be, but, by, for, if, in, into, is, it, no, not, of, on, or,
# ...). It omits common action verbs that appear in virtually every Nauro
# decision title ("Use Postgres", "Use Redis", "Use FastAPI", etc.), so a
# fresh proposal shares the stem ``use`` with most existing decisions and
# gets a nonzero BM25 score, escalating tier 2 → tier 3 on almost every
# call. Extending the list with ``use`` collapses those false-positive
# matches to score 0 (already filtered by bm25_retrieve).
#
# This list is intentionally minimal: only tokens with observed false
# positives are added, not a speculative blanket of generic verbs. Add
# more only when a concrete failure case justifies it, and update
# test_tier2.py to lock the case in.
_TIER2_STOPWORDS = list(STOPWORDS_EN) + ["use"]

# The scaffold-seeded "Initial project setup" decision is Nauro's own
# bookkeeping — it records that the store was initialized, not a choice the
# user made. It should not gate validation of user-authored proposals.
# Including it in the tier-2 corpus causes every new proposal sharing even
# one weak stem (e.g. "store", "use") with the template text to escalate
# to tier 3, defeating tier 2's purpose as a cheap pre-filter.
#
# Identified by the conventional num+title pair written by
# ``scaffold_project_store`` via ``FIRST_DECISION_MD``. This is a convention
# match, not a fuzzy heuristic: the title is hardcoded in the scaffold
# template and the scaffold is the only path that produces a ``num == 1``
# decision with this exact title.
_SCAFFOLD_SEED_TITLE = "Initial project setup"


def _is_scaffold_seed(decision: dict) -> bool:
    return decision.get("num") == 1 and decision.get("title") == _SCAFFOLD_SEED_TITLE


def check_similarity(proposal: dict, project_path: Path) -> tuple[str, list[dict]]:
    """Check proposal similarity against existing decisions using BM25.

    Returns:
        (action, similar_decisions) where action is "auto_confirm" or "needs_review".
    """
    decisions = [d for d in _list_decisions(project_path) if not _is_scaffold_seed(d)]
    if not decisions:
        return ("auto_confirm", [])

    title = proposal.get("title", "")
    rationale = proposal.get("rationale", "")
    proposal_text = f"{title}. {rationale[:200]}"

    related = bm25_retrieve(decisions, proposal_text, top_k=TOP_K, stopwords=_TIER2_STOPWORDS)
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
