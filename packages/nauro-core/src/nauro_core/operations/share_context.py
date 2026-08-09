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
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nauro_core.constants import MAX_BRIEF_BYTES, POINTER_PREFIX_BY_KIND
from nauro_core.identifiers import IdentifierKind, InvalidIdentifier, validate_identifier
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes

# Per-field cap on the single-line pointer summary. The composed pointer
# body (prefix + path + separator + summary) stays well under
# MAX_QUESTION_LENGTH by construction of this cap and the slug bound.
SUMMARY_CHAR_LIMIT = 300


def brief_path(slug: str) -> str:
    """Return the store-relative brief path for a validated *slug*."""
    return f"context/{slug}.md"


def compose_pointer_body(pointer_kind: str, path: str, summary: str) -> str:
    """Compose the canonical discovery-pointer body with the ASCII separator."""
    return f"{POINTER_PREFIX_BY_KIND[pointer_kind]} {path} - {summary}"


class ShareContextPlan(BaseModel):
    """Frozen execution plan for one accepted ``share_context`` payload.

    ``content`` is the validated immutable brief body; the server stores its
    exact UTF-8 encoding create-only at ``path``. ``content_digest`` is the
    lowercase SHA-256 hex digest of those bytes. ``pointer_body`` is the
    frozen canonical discovery-pointer text — the question number in front
    of it is allocated (and on contention reallocated) at execution, never
    here. ``payload_bytes`` is the canonical JSON payload encoding — the
    single-sourced idempotency digest input.
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
    """Validate a shared-brief payload and return its frozen plan.

    Args:
        slug: Brief name, 1 to 120 lowercase ASCII letters, digits, or
            internal hyphens; maps only to ``context/<slug>.md``.
        content: The immutable brief body. Must be UTF-8-encodable text
            without NUL characters, at most
            :data:`~nauro_core.constants.MAX_BRIEF_BYTES` encoded bytes.
        pointer_kind: One of ``brief``, ``resume``, or ``selection`` —
            selects the discovery-pointer prefix.
        summary: Nonempty single line of at most
            :data:`SUMMARY_CHAR_LIMIT` characters, carried in the composed
            pointer body.

    Returns:
        :class:`ShareContextPlan` on acceptance.

    Raises:
        PlanRejected: On any Tier-1 validation failure. No plan exists and
            nothing may be written.
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
