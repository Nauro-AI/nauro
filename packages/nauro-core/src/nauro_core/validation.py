"""Structural screening, similarity, and hash-based deduplication.

Pure validation functions that determine whether a candidate decision should
be written to the store. No I/O — existing decisions and hashes are passed
in by callers.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

from nauro_core.constants import (
    MIN_RATIONALE_LENGTH,
    VALID_CONFIDENCES,
)
from nauro_core.decision_model import (
    DECISION_TYPE_VALUES,
    DecisionConfidence,
    DecisionSource,
    Reversibility,
)
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
#
# Built lazily (module __getattr__ below) so importing the package does not
# pull the bm25s/numpy stack; idle MCP server processes never search. Cached
# so every access shares one list object, matching the old module constant.
@lru_cache(maxsize=1)
def _tier2_stopwords() -> list[str]:
    from bm25s.stopwords import STOPWORDS_EN

    return [*STOPWORDS_EN, "use"]


def __getattr__(name: str) -> list[str]:
    if name == "TIER2_STOPWORDS":
        return _tier2_stopwords()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    """Detect the scaffold-seeded first decision: ``num == 1`` plus the exact
    scaffold title. Accepts a ``Decision`` object or the legacy dict shape.
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
    """Tier-2 BM25 similarity check, shared by the local and remote surfaces so the
    same proposal validates identically. Filters the scaffold seed, then delegates to
    ``bm25_retrieve``; returns ``("auto_confirm" | "needs_review", hits)`` in its shape.
    """
    candidates = [d for d in existing_decisions if not is_scaffold_seed(d)]
    if not candidates:
        return ("auto_confirm", [])

    proposal_text = f"{proposal.get('title', '')}. {(proposal.get('rationale') or '')[:200]}"
    related = bm25_retrieve(
        candidates,
        proposal_text,
        top_k=top_k,
        stopwords=stopwords if stopwords is not None else _tier2_stopwords(),
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


def normalize_title(title: str) -> str:
    """Collapse whitespace and lowercase for title comparison."""
    return " ".join(title.lower().split())


def rejected_item_label(item: dict) -> str | None:
    """Return the first non-empty stripped value under ``alternative``, then the
    legacy ``name`` alias; None when neither key carries a label.
    """
    for key in ("alternative", "name"):
        value = item.get(key)
        if value is None:
            continue
        label = str(value).strip()
        if label:
            return label
    return None


# Optional enum-valued proposal fields and their allowed wire values. Each
# must reject at Tier 1: the write path coerces them with the enum
# constructor, so a non-member value that slips past screening raises an
# uncaught ValueError after validation has already passed.
_OPTIONAL_ENUM_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("decision_type", DECISION_TYPE_VALUES),
    ("reversibility", tuple(r.value for r in Reversibility)),
    ("source", tuple(s.value for s in DecisionSource)),
)


def screen_structural(
    proposal: dict,
    existing_hashes: set[str],
    active_decisions: list[dict],
) -> tuple[str, str | None]:
    """Structural Tier-1 screen, no I/O: schema and enum fields, minimum rationale
    length, rejected-item labels, hash dedup, and title dedup against the caller-filtered
    ``active_decisions`` (operation-agnostic). Returns ``("pass" | "reject", reason)``.
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

    # None and blank strings coerce to unset on the write path, so only a
    # non-blank non-member value rejects.
    for field_name, allowed in _OPTIONAL_ENUM_FIELDS:
        raw = proposal.get(field_name)
        if raw is None:
            continue
        value = raw.strip() if isinstance(raw, str) else str(raw)
        if value and value not in allowed:
            return (
                "reject",
                f"Invalid {field_name}: {value}. Must be one of: {', '.join(allowed)}",
            )

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
    title_normalized = normalize_title(title)
    for d in active_decisions:
        if hasattr(d, "title"):
            existing_title = d.title
            existing_num = d.num
        else:
            existing_title = d.get("title", "")
            existing_num = d.get("num", "?")
        if normalize_title(existing_title) == title_normalized:
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
    """Return the first envelope token present in *text*, or ``None``.

    A literal substring scan over a small closed token set. Plain string ops only.
    """
    if not text:
        return None
    for token in _ENVELOPE_TOKENS:
        if token in text:
            return token
    return None


def envelope_token_message(value: str, field_name: str) -> str | None:
    """Return the rejection reason if *value* carries an envelope fragment, else None.

    Wraps :func:`find_envelope_token`, naming *field_name* and the detected token.
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
