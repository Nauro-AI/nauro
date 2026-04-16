"""Structural screening, similarity, and hash-based deduplication.

Pure validation functions that determine whether a candidate decision should
be written to the store. No I/O — existing decisions and hashes are passed
in by callers.
"""

from __future__ import annotations

import hashlib
import re

from bm25s.stopwords import STOPWORDS_EN

from nauro_core.constants import (
    MIN_RATIONALE_LENGTH,
    VALID_CONFIDENCES,
)
from nauro_core.search import bm25_retrieve

# Tier-2 BM25 defaults shared by local (nauro) and remote (mcp-server) surfaces.
# Both call check_bm25_similarity below so the same proposal produces the same
# validation outcome on either.
TIER2_TOP_K = 5

# bm25s's default English stopword list is minimal (~30 tokens: a, an, and,
# are, as, at, be, but, by, for, if, in, into, is, it, no, not, of, on, or,
# ...). It omits common action verbs that appear in virtually every Nauro
# decision title ("Use Postgres", "Use Redis", "Use FastAPI", etc.), so a
# fresh proposal shares the stem ``use`` with most existing decisions and
# gets a nonzero BM25 score, escalating tier 2 -> tier 3 on almost every
# call. Extending the list with ``use`` collapses those false-positive
# matches to score 0 (already filtered by bm25_retrieve).
#
# This list is intentionally minimal: only tokens with observed false
# positives are added, not a speculative blanket of generic verbs. Add
# more only when a concrete failure case justifies it, and add a test to
# lock the case in.
TIER2_STOPWORDS = list(STOPWORDS_EN) + ["use"]

# The scaffold-seeded "Initial project setup" decision is Nauro's own
# bookkeeping — it records that the store was initialized, not a choice the
# user made. It should not gate validation of user-authored proposals.
# Including it in the tier-2 corpus causes every new proposal sharing even
# one weak stem (e.g. "store", "use") with the template text to escalate,
# defeating tier 2's purpose as a cheap pre-filter.
#
# Identified by the conventional num+title pair written by the scaffold
# template. This is a convention match, not a fuzzy heuristic: the title is
# hardcoded in the scaffold and the scaffold is the only path that produces
# a ``num == 1`` decision with this exact title.
_SCAFFOLD_SEED_TITLE = "Initial project setup"


def _is_scaffold_seed(decision: dict) -> bool:
    return decision.get("num") == 1 and decision.get("title") == _SCAFFOLD_SEED_TITLE


def check_bm25_similarity(
    proposal: dict,
    existing_decisions: list[dict],
    top_k: int = TIER2_TOP_K,
    stopwords: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """Tier-2 BM25 similarity check (D93). Shared by local and remote surfaces.

    Filters the scaffold-seed decision (bookkeeping, not a user choice) and
    delegates retrieval to ``nauro_core.search.bm25_retrieve``, which also
    filters non-active decisions and empty-score matches.

    Args:
        proposal: Dict with at least "title" and "rationale" keys.
        existing_decisions: Parsed decision dicts. Each should carry "num",
            "title", "rationale", and optionally "status".
        top_k: Maximum number of related decisions to return.
        stopwords: Override for tokenizer stopwords. Defaults to
            ``TIER2_STOPWORDS``.

    Returns:
        (action, related) where action is "auto_confirm" or "needs_review"
        and related uses the ``bm25_retrieve`` shape:
        ``{"number", "title", "similarity", "rationale_preview"}``.
    """
    candidates = [d for d in existing_decisions if not _is_scaffold_seed(d)]
    if not candidates:
        return ("auto_confirm", [])

    proposal_text = f"{proposal.get('title', '')}. {(proposal.get('rationale') or '')[:200]}"
    related = bm25_retrieve(
        candidates,
        proposal_text,
        top_k=top_k,
        stopwords=stopwords if stopwords is not None else TIER2_STOPWORDS,
    )
    if not related:
        return ("auto_confirm", [])
    return ("needs_review", related)


def check_content_length(value: str, label: str, max_length: int) -> str | None:
    """Return an error message if *value* exceeds *max_length*, else None."""
    if len(value) > max_length:
        return f"{label} exceeds maximum length of {max_length} characters (got {len(value)})."
    return None


def compute_hash(title: str, rationale: str) -> str:
    """SHA-256 of normalized title + rationale for exact dedup."""
    content = f"{title.strip().lower()}|{rationale.strip().lower()}"
    return hashlib.sha256(content.encode()).hexdigest()


def _normalize_title(title: str) -> str:
    """Collapse whitespace and lowercase for title comparison."""
    return re.sub(r"\s+", " ", title.lower().strip())


def screen_structural(
    proposal: dict,
    existing_hashes: set[str],
    recent_decisions: list[dict],
) -> tuple[str, str | None]:
    """Run structural screening on a proposal. No I/O.

    Checks: schema validation (title, rationale, confidence), minimum rationale
    length, hash dedup against existing_hashes, and title dedup against
    recent_decisions (same title within 24h — caller filters by recency).

    Args:
        proposal: Dict with title, rationale, confidence keys.
        existing_hashes: Set of content hashes from the hash index.
        recent_decisions: Decisions from the last 24 hours (caller filters).

    Returns:
        (action, reason) where action is "pass" or "reject".
    """
    title = (proposal.get("title") or "").strip()
    if not title:
        return ("reject", "Title is empty.")

    rationale = (proposal.get("rationale") or "").strip()
    if not rationale:
        return ("reject", "Rationale is empty.")

    confidence = proposal.get("confidence", "medium")
    if confidence not in VALID_CONFIDENCES:
        return ("reject", f"Invalid confidence: {confidence}. Must be one of: {VALID_CONFIDENCES}")

    if len(rationale) < MIN_RATIONALE_LENGTH:
        return (
            "reject",
            f"Rationale too short ({len(rationale)} chars). Minimum {MIN_RATIONALE_LENGTH}.",
        )

    # Hash dedup
    content_hash = compute_hash(title, rationale)
    if content_hash in existing_hashes:
        return ("reject", "Exact duplicate of existing decision (hash match).")

    # Title dedup against recent decisions (caller provides 24h window)
    title_normalized = _normalize_title(title)
    for d in recent_decisions:
        existing_title = d.get("title", "")
        existing_normalized = _normalize_title(existing_title)
        if existing_normalized == title_normalized:
            return (
                "reject",
                f"Decision with same title written recently: D{d.get('num', '?')}",
            )

    return ("pass", None)
