"""``update_stack`` — plan the full replacement of ``stack.md``.

A plan-returning kernel operation (see :mod:`nauro_core.operations.planning`
for the convention): unlike the Store-writing operations in this package it
performs no I/O. The kernel validates the complete replacement document,
computes the deterministic content revision, and returns a frozen
:class:`UpdateStackPlan`; the hosted server observes current state, enforces
the revision precondition, and executes the storage writes. Preconditions
are expressed as content digests or the absent token only — S3 ETags never
enter the kernel.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nauro_core.constants import STACK_DOC_CHAR_LIMIT, STACK_MD, STACK_REVISION_ABSENT
from nauro_core.identifiers import IdentifierKind, InvalidIdentifier, validate_identifier
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes


def compute_stack_revision(content: bytes | None) -> str:
    """Return the stack revision for *content*.

    The lowercase SHA-256 hex digest of the exact bytes; ``None`` (no file)
    returns the literal absent token. Every surface that forms or checks a
    stack precondition computes the revision through this function.
    """
    if content is None:
        return STACK_REVISION_ABSENT
    return hashlib.sha256(content).hexdigest()


class UpdateStackPlan(BaseModel):
    """Frozen execution plan for one accepted ``update_stack`` payload.

    ``content`` is the validated complete replacement document; the server
    stores its exact UTF-8 encoding with no normalization. ``new_revision``
    is the revision the store carries after the write commits.
    ``expected_revision`` echoes the caller's precondition (a revision hex
    digest, the absent token for create-only, or ``None`` when no
    precondition was supplied). ``payload_bytes`` is the canonical JSON
    payload encoding — the single-sourced idempotency digest input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["update_stack"] = "update_stack"
    path: Literal["stack.md"] = STACK_MD
    content: str
    new_revision: str
    expected_revision: str | None
    payload_bytes: bytes


def update_stack(content: str, expected_revision: str | None = None) -> UpdateStackPlan:
    """Validate a full stack.md replacement and return its frozen plan.

    Args:
        content: The complete replacement document. Must be UTF-8-encodable
            Markdown text without NUL characters, at most
            :data:`~nauro_core.constants.STACK_DOC_CHAR_LIMIT` characters.
        expected_revision: Optional precondition — the stack revision the
            caller observed, or the absent token when creating a previously
            missing file. Validated but never resolved here; the server
            compares it against the current revision at execution.

    Returns:
        :class:`UpdateStackPlan` on acceptance.

    Raises:
        PlanRejected: On any Tier-1 validation failure. No plan exists and
            nothing may be written.
    """
    if "\x00" in content:
        raise PlanRejected("content contains a NUL character.")
    if len(content) > STACK_DOC_CHAR_LIMIT:
        raise PlanRejected(
            f"content exceeds the {STACK_DOC_CHAR_LIMIT}-character stack.md "
            f"document cap (got {len(content)})."
        )
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanRejected("content is not valid UTF-8-encodable text.") from None

    if expected_revision is not None:
        try:
            validate_identifier(
                IdentifierKind.stack_revision, expected_revision, field="expected_revision"
            )
        except InvalidIdentifier as exc:
            raise PlanRejected(str(exc)) from None

    return UpdateStackPlan(
        content=content,
        new_revision=compute_stack_revision(content_bytes),
        expected_revision=expected_revision,
        payload_bytes=canonical_payload_bytes(
            {
                "operation": "update_stack",
                "content": content,
                "expected_revision": expected_revision,
            }
        ),
    )
