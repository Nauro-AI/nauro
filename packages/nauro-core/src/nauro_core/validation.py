"""Structural screening, Jaccard similarity, and hash-based deduplication.

Pure validation functions that determine whether a candidate decision should
be written to the store. No I/O — existing decisions and hashes are passed
in by callers.
"""

from __future__ import annotations

import hashlib
import re

from nauro_core.constants import (
    JACCARD_THRESHOLD,
    MIN_RATIONALE_LENGTH,
    VALID_CONFIDENCES,
)


def check_content_length(value: str, label: str, max_length: int) -> str | None:
    """Return an error message if *value* exceeds *max_length*, else None."""
    if len(value) > max_length:
        return f"{label} exceeds maximum length of {max_length} characters (got {len(value)})."
    return None


def compute_hash(title: str, rationale: str) -> str:
    """SHA-256 of normalized title + rationale for exact dedup."""
    content = f"{title.strip().lower()}|{rationale.strip().lower()}"
    return hashlib.sha256(content.encode()).hexdigest()


def word_set(text: str) -> set[str]:
    """Extract a set of lowercase words (len > 2) from text."""
    return {w.lower().strip(".,;:!?()[]{}\"'") for w in text.split() if len(w) > 2}


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard index between two word sets. Returns 0.0 if both are empty."""
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def check_jaccard_similarity(
    proposal: dict,
    existing_decisions: list[dict],
    threshold: float = JACCARD_THRESHOLD,
) -> tuple[str, list[dict]]:
    """Check proposal against existing decisions using Jaccard word-set similarity.

    Args:
        proposal: Dict with at least "title" and "rationale" keys.
        existing_decisions: List of parsed decision dicts.
        threshold: Similarity threshold (default from constants).

    Returns:
        (action, similar_decisions) where action is "auto_confirm" or "needs_review".
        similar_decisions is sorted by similarity descending, capped at 5.
    """
    if not existing_decisions:
        return ("auto_confirm", [])

    proposal_text = f"{proposal.get('title', '')}. {(proposal.get('rationale') or '')[:200]}"
    proposal_words = word_set(proposal_text)
    if not proposal_words:
        return ("auto_confirm", [])

    similarities: list[dict] = []
    for d in existing_decisions:
        decision_text = f"{d.get('title', '')}. {(d.get('rationale') or '')[:200]}"
        decision_words = word_set(decision_text)
        sim = jaccard_similarity(proposal_words, decision_words)

        if sim >= threshold:
            similarities.append(
                {
                    "number": d.get("num", 0),
                    "title": d.get("title", ""),
                    "similarity": round(sim, 3),
                }
            )

    if not similarities:
        return ("auto_confirm", [])

    similarities.sort(key=lambda x: x["similarity"], reverse=True)
    return ("needs_review", similarities[:5])


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
