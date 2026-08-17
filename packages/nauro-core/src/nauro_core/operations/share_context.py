"""``share_context`` — plan one immutable brief and its discovery pointer.

A plan-returning kernel operation (see :mod:`nauro_core.operations.planning`
for the convention): unlike the Store-writing operations in this package it
performs no I/O. The kernel validates the brief payload, derives the brief
path, content digest, and canonical pointer body, and returns a frozen
:class:`ShareContextPlan`; the hosted server observes current state, checks
slug collisions, allocates the question number through
:mod:`nauro_core.question_append` (the same composer ``flag_question``
appends with), and executes the storage writes. Preconditions are expressed
as content digests or path absence only — S3 ETags never enter the kernel.

The canonical pointer writer uses the ASCII ``" - "`` separator; readers
keep accepting the deployed em-dash form indefinitely, so existing entries
stay byte-identical.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nauro_core.constants import MAX_BRIEF_BYTES, POINTER_PREFIX_BY_KIND
from nauro_core.identifiers import IdentifierKind, InvalidIdentifier, validate_identifier
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes

# Per-field cap on the single-line pointer summary. The composed pointer
# body (prefix + path + separator + summary) stays well under
# MAX_QUESTION_LENGTH by construction of this cap and the slug bound.
SUMMARY_CHAR_LIMIT = 300

# Unicode general categories a summary may never carry: control characters
# (Cc, which covers tab and ESC) plus the line and paragraph separators
# (Zl, Zp). Together these are every separator str.splitlines recognises
# beyond \n and \r, so an accepted summary stays one entry line for every
# reader of open-questions.md, not only for the writer that appends it.
_FORBIDDEN_SUMMARY_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})

# Bidirectional controls reorder rendered text without appearing in it, so a
# summary carrying one can display differently from the bytes an auditor
# reads. They are format characters (Cf), which the visible-content rule
# below strips rather than rejects, so they are named explicitly.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

# Category prefixes that count as visible content: letters, numbers, symbols,
# and punctuation. A summary of only format characters, whitespace, or
# combining marks renders blank in the pointer entry.
_VISIBLE_CATEGORY_PREFIXES = frozenset({"L", "N", "S", "P"})

# Default-ignorable code points Unicode classifies as letters (Lo), so the
# category test alone would count them as visible even though they render as
# nothing. unicodedata exposes no Default_Ignorable_Code_Point property, but
# the enumeration is complete rather than partial: every other default-ignorable
# code point is already Cf, Mn, or Cn, none of which carries a visible category
# prefix. Recheck this set when the bundled Unicode data gains a new one.
_INVISIBLE_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")


def brief_path(slug: str) -> str:
    """Return the store-relative brief path for a validated *slug*."""
    return f"context/{slug}.md"


def compose_pointer_body(pointer_kind: str, path: str, summary: str) -> str:
    """Compose the canonical discovery-pointer body with the ASCII separator."""
    return f"{POINTER_PREFIX_BY_KIND[pointer_kind]} {path} - {summary}"


def _reject_hidden_summary_characters(summary: str) -> None:
    """Reject *summary* if it carries a control, separator, or bidi character."""
    for char in summary:
        if unicodedata.category(char) in _FORBIDDEN_SUMMARY_CATEGORIES:
            raise PlanRejected(
                f"summary contains a control or separator character (U+{ord(char):04X}); "
                "it must be one line of plain text."
            )
        if char in _BIDI_CONTROLS:
            raise PlanRejected(
                f"summary contains a bidirectional control character (U+{ord(char):04X}); "
                "it must read the same stored as rendered."
            )


def _reject_summary_without_visible_content(summary: str) -> None:
    """Reject *summary* if nothing renders once formatting is stripped."""
    for char in summary:
        category = unicodedata.category(char)
        if category == "Cf" or char.isspace() or char in _INVISIBLE_LETTERS:
            continue
        if category[0] in _VISIBLE_CATEGORY_PREFIXES:
            return
    raise PlanRejected("summary has no visible characters.")


class ShareContextPlan(BaseModel):
    """Frozen execution plan for one accepted ``share_context`` payload.

    ``content`` is the validated brief body, stored create-only at ``path`` in exact
    UTF-8, and ``content_digest`` is its lowercase SHA-256. ``pointer_body`` is
    frozen text numbered at execution; ``payload_bytes`` is the idempotency input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["share_context"] = "share_context"
    slug: str
    path: str
    content: str
    content_digest: str
    pointer_kind: Literal["brief", "resume", "selection"]
    summary: str
    pointer_body: str
    payload_bytes: bytes


def share_context(
    slug: str,
    content: str,
    pointer_kind: str,
    summary: str,
) -> ShareContextPlan:
    """Validate a shared-brief payload into a frozen plan: ``slug`` maps only to
    ``context/<slug>.md``, ``pointer_kind`` is ``brief``, ``resume`` or ``selection``, and
    ``content`` and the one-line ``summary`` are bounded clean UTF-8, else ``PlanRejected``.
    """
    try:
        validate_identifier(IdentifierKind.brief_slug, slug, field="slug")
    except InvalidIdentifier as exc:
        raise PlanRejected(str(exc)) from None

    if "\x00" in content:
        raise PlanRejected("content contains a NUL character.")
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanRejected("content is not valid UTF-8-encodable text.") from None
    if len(content_bytes) > MAX_BRIEF_BYTES:
        raise PlanRejected(
            f"content exceeds the {MAX_BRIEF_BYTES}-byte brief cap "
            f"(got {len(content_bytes)} bytes)."
        )

    if pointer_kind not in POINTER_PREFIX_BY_KIND:
        raise PlanRejected(f"pointer_kind must be one of: {', '.join(POINTER_PREFIX_BY_KIND)}.")

    if not summary.strip():
        raise PlanRejected("summary is empty.")
    if "\n" in summary or "\r" in summary:
        raise PlanRejected("summary must be a single line.")
    if len(summary) > SUMMARY_CHAR_LIMIT:
        raise PlanRejected(
            f"summary exceeds the {SUMMARY_CHAR_LIMIT}-character cap (got {len(summary)})."
        )
    try:
        summary.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanRejected("summary is not valid UTF-8-encodable text.") from None
    _reject_hidden_summary_characters(summary)
    _reject_summary_without_visible_content(summary)

    path = brief_path(slug)
    return ShareContextPlan(
        slug=slug,
        path=path,
        content=content,
        content_digest=hashlib.sha256(content_bytes).hexdigest(),
        pointer_kind=pointer_kind,
        summary=summary,
        pointer_body=compose_pointer_body(pointer_kind, path, summary),
        payload_bytes=canonical_payload_bytes(
            {
                "operation": "share_context",
                "slug": slug,
                "content": content,
                "pointer_kind": pointer_kind,
                "summary": summary,
            }
        ),
    )
