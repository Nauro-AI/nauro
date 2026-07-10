"""Structural screening, similarity, and hash-based deduplication.

Pure validation functions that determine whether a candidate decision should
be written to the store. No I/O — existing decisions and hashes are passed
in by callers.
"""

from __future__ import annotations

import hashlib

from bm25s.stopwords import STOPWORDS_EN

from nauro_core.constants import (
    MIN_RATIONALE_LENGTH,
    VALID_CONFIDENCES,
)
from nauro_core.decision_model import DecisionConfidence
from nauro_core.search import Bm25Hit, bm25_retrieve

# Tier-2 BM25 defaults shared by local (nauro) and remote (mcp-server) surfaces.
# Both call check_bm25_similarity below so the same proposal produces the same
# validation outcome on either.
TIER2_TOP_K = 5

# bm25s's default English stopword list is minimal (~30 tokens: a, an, and,
# are, as, at, be, but, by, for, if, in, into, is, it, no, not, of, on, or,
# ...). It omits common action verbs that appear in virtually every Nauro
# decision title ("Use Postgres", "Use Redis", "Use FastAPI", etc.), so a
# fresh proposal shares the stem ``use`` with most existing decisions and
# gets a nonzero BM25 score, surfacing as a near-neighbour on almost every
# call. Extending the list with ``use`` collapses those false-positive
# matches to score 0 (already filtered by bm25_retrieve).
#
# This list is intentionally minimal: only tokens with observed false
# positives are added, not a speculative blanket of generic verbs. Add
# more only when a concrete failure case justifies it, and add a test to
# lock the case in.
TIER2_STOPWORDS = [*list(STOPWORDS_EN), "use"]

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


def is_scaffold_seed(decision) -> bool:
    """Detect the scaffold-seeded first decision (``Decision`` or legacy dict).

    The convention match (``num == 1`` and the exact scaffold title) is the
    single source every surface shares: tier-2 validation, ``check_decision``
    retrieval, and the graph payload builder all call this predicate so they
    agree on what counts as the seed.
    """
    if hasattr(decision, "num"):
        return decision.num == 1 and decision.title == _SCAFFOLD_SEED_TITLE
    return decision.get("num") == 1 and decision.get("title") == _SCAFFOLD_SEED_TITLE


def check_bm25_similarity(
    proposal: dict,
    existing_decisions: list,
    top_k: int = TIER2_TOP_K,
    stopwords: list[str] | None = None,
) -> tuple[str, list[Bm25Hit]]:
    """Tier-2 BM25 similarity check. Shared by local and remote surfaces.

    Filters the scaffold-seed decision (bookkeeping, not a user choice) and
    delegates retrieval to ``nauro_core.search.bm25_retrieve``, which also
    filters non-active decisions and empty-score matches.

    Args:
        proposal: Dict with at least "title" and "rationale" keys (extractor /
            MCP input shape — stays a dict).
        existing_decisions: Parsed ``Decision`` objects.
        top_k: Maximum number of related decisions to return.
        stopwords: Override for tokenizer stopwords. Defaults to
            ``TIER2_STOPWORDS``.

    Returns:
        (action, related) where action is "auto_confirm" or "needs_review"
        and related uses the ``bm25_retrieve`` shape:
        ``{"number", "title", "similarity", "rationale_preview"}``.
    """
    candidates = [d for d in existing_decisions if not is_scaffold_seed(d)]
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
    return " ".join(title.lower().split())


def rejected_item_label(item: dict) -> str | None:
    """Extract the label from a dict-form rejected-alternative item.

    The label is the first value under ``alternative`` then the legacy
    ``name`` alias that is non-empty after stripping; an empty
    ``alternative`` falls through to ``name``. Returns the stripped label,
    or None when neither key carries one. Shared by the Tier 1 screen and
    the write-path coercer so both agree on what counts as labeled.
    """
    for key in ("alternative", "name"):
        value = item.get(key)
        if value is None:
            continue
        label = str(value).strip()
        if label:
            return label
    return None


def screen_structural(
    proposal: dict,
    existing_hashes: set[str],
    active_decisions: list[dict],
) -> tuple[str, str | None]:
    """Run structural screening on a proposal. No I/O.

    Checks: schema validation (title, rationale, confidence), minimum rationale
    length, rejected-alternative labels (each dict item must carry a non-empty
    'alternative' or 'name'), hash dedup against existing_hashes, and title
    dedup against active_decisions (same title as a decision still in force).

    This function is operation-agnostic: it dedups the proposal title against
    whatever list the caller hands it. The caller decides which decisions are
    eligible (active decisions, with any supersede target excluded).

    Args:
        proposal: Dict with title, rationale, confidence keys.
        existing_hashes: Set of content hashes from the hash index.
        active_decisions: Decisions to dedup the title against (caller filters
            to active decisions, excluding any supersede target).

    Returns:
        (action, reason) where action is "pass" or "reject".
    """
    title = (proposal.get("title") or "").strip()
    if not title:
        return ("reject", "Title is empty.")

    rationale = (proposal.get("rationale") or "").strip()
    if not rationale:
        return ("reject", "Rationale is empty.")

    confidence = proposal.get("confidence", DecisionConfidence.medium.value)
    if confidence not in VALID_CONFIDENCES:
        return ("reject", f"Invalid confidence: {confidence}. Must be one of: {VALID_CONFIDENCES}")

    if len(rationale) < MIN_RATIONALE_LENGTH:
        return (
            "reject",
            f"Rationale too short ({len(rationale)} chars). Minimum {MIN_RATIONALE_LENGTH}.",
        )

    # Dict-form rejected items must carry a label; a nameless item used to
    # silently default its heading to "Unknown" on the write path. The
    # message echoes key names only — never the item's values.
    for idx, item in enumerate(proposal.get("rejected") or ()):
        if isinstance(item, dict) and rejected_item_label(item) is None:
            return (
                "reject",
                f"rejected[{idx}] has no label: expected a non-empty "
                f"'alternative' (or 'name') key; got keys {list(item.keys())}.",
            )

    # Hash dedup
    content_hash = compute_hash(title, rationale)
    if content_hash in existing_hashes:
        return ("reject", "Exact duplicate of existing decision (hash match).")

    # Title dedup against active decisions (caller filters to active, minus any
    # supersede target). Entries may be either Decision objects or lightweight
    # dicts (the mcp-server tier-1 path loads just title+num from S3 without
    # full parsing). Handle both shapes.
    title_normalized = _normalize_title(title)
    for d in active_decisions:
        if hasattr(d, "title"):
            existing_title = d.title
            existing_num = d.num
        else:
            existing_title = d.get("title", "")
            existing_num = d.get("num", "?")
        if _normalize_title(existing_title) == title_normalized:
            return (
                "reject",
                f"An active decision already has this title: D{existing_num}. "
                'Use operation="supersede" to replace it, or operation="update" '
                "to append rationale — not a second add.",
            )

    return ("pass", None)


# Some agent surfaces emit tool calls as XML and their MCP bridges may
# fail to extract <parameter> values cleanly, so the envelope tail
# (</question>, <parameter name="context">, </invoke>, etc.) ends up
# appended to the string field the server receives. Writers must reject
# these before they hit disk.
_ENVELOPE_TOKENS: tuple[str, ...] = (
    "</question>",
    "</rationale>",
    "</context>",
    "</parameter>",
    "</invoke>",
    "<parameter name=",
    "<invoke name=",
)


def find_envelope_token(text: str) -> str | None:
    """Return the first envelope token present in *text*, or None.

    The check is a literal substring scan against a small, closed token set
    — these are tool-use envelope fragments that must never reach the store.
    Plain string operations only.
    """
    if not text:
        return None
    for token in _ENVELOPE_TOKENS:
        if token in text:
            return token
    return None


def envelope_token_message(value: str, field_name: str) -> str | None:
    """Return the rejection reason if *value* carries an envelope fragment, else None.

    Wraps ``find_envelope_token``: on a hit, returns the full reason string
    naming the offending *field_name* and the detected token; on no hit,
    returns None. Both MCP transports share this builder so the message agents
    see does not drift by transport. Pure — no I/O.
    """
    token = find_envelope_token(value)
    if not token:
        return None
    return (
        f"{field_name} contains tool-use envelope fragment {token!r}. "
        "This usually means the client failed to extract the parameter "
        "value cleanly from an XML tool call. Resend the call with just "
        "the prose content."
    )
