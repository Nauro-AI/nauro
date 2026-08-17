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
    """Return the stack revision for *content*: the lowercase SHA-256 hex digest of
    the exact bytes, or the literal absent token for ``None``. Every surface that
    forms or checks a stack precondition computes the revision here.
    """
    if content is None:
        return STACK_REVISION_ABSENT
    return hashlib.sha256(content).hexdigest()


class UpdateStackPlan(BaseModel):
    """Frozen execution plan for one accepted ``update_stack`` payload.

    ``content`` is the validated replacement document, stored in exact UTF-8 with no
    normalization. ``new_revision`` is what the store carries after the commit,
    ``expected_revision`` the caller's precondition, ``payload_bytes`` the digest input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["update_stack"] = "update_stack"
    path: Literal["stack.md"] = STACK_MD
    content: str
    new_revision: str
    expected_revision: str | None
    payload_bytes: bytes


def update_stack(content: str, expected_revision: str | None = None) -> UpdateStackPlan:
    """Validate a full stack.md replacement into a frozen plan: ``content`` must be clean
    UTF-8 Markdown within ``STACK_DOC_CHAR_LIMIT``, and ``expected_revision`` is
    validated but compared later. ``PlanRejected`` means no plan and nothing writable.
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
