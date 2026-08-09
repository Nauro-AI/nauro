"""Shared shape validators for identifiers that cross the local/hosted seam.

The provenance ruling requires one definition of every new identifier shape,
used by local and hosted parsing alike, so journal targets, receipts,
projection rows, and audit records cannot drift apart and every encoded
identifier has a predictable maximum size. The alphabet-only local project-ID
check is deliberately not reused here.

The kinds:

- ``ulid`` - server-generated identifiers (proposal, question event, report
  event, audit event, receipt, generation, invitation): 26-character
  uppercase Crockford ULIDs.
- ``operation_id`` - client-supplied operation identifiers: 1 to 128
  printable ASCII characters.
- ``server_token`` - closed server-owned tokens (operation kinds, audit
  actions, target kinds, system reasons, execution modes, transition
  values): a lowercase letter followed by lowercase letters, digits,
  or underscores, at most 64 characters.
- ``audit_target_id`` - stable opaque audit target identifiers: 1 to 128
  printable ASCII characters, never an alias or free text.
- ``brief_slug`` - shared-context brief names mapping to
  ``context/<slug>.md``: 1 to 120 lowercase ASCII letters and digits with
  internal hyphens only (never leading or trailing).
- ``stack_revision`` - stack.md content revisions: a lowercase 64-character
  SHA-256 hex digest, or the literal absent token for a missing file.

Matching is always whole-value, so a trailing newline never passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from nauro_core.constants import BRIEF_SLUG_MAX_LENGTH, STACK_REVISION_ABSENT


class IdentifierKind(str, Enum):
    ulid = "ulid"
    operation_id = "operation_id"
    server_token = "server_token"
    audit_target_id = "audit_target_id"
    brief_slug = "brief_slug"
    stack_revision = "stack_revision"


@dataclass(frozen=True)
class _Shape:
    pattern: re.Pattern[str]
    description: str


_PRINTABLE_ASCII = _Shape(
    re.compile(r"[\x20-\x7e]{1,128}"),
    "1 to 128 printable ASCII characters",
)

_SHAPES: dict[IdentifierKind, _Shape] = {
    IdentifierKind.ulid: _Shape(
        re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}"),
        "a 26-character uppercase Crockford ULID",
    ),
    IdentifierKind.operation_id: _PRINTABLE_ASCII,
    IdentifierKind.server_token: _Shape(
        re.compile(r"[a-z][a-z0-9_]{0,63}"),
        "a lowercase server-owned token of at most 64 characters, starting with a letter",
    ),
    IdentifierKind.audit_target_id: _PRINTABLE_ASCII,
    IdentifierKind.brief_slug: _Shape(
        re.compile(rf"[a-z0-9](?:[a-z0-9-]{{0,{BRIEF_SLUG_MAX_LENGTH - 2}}}[a-z0-9])?"),
        f"1 to {BRIEF_SLUG_MAX_LENGTH} lowercase ASCII letters, digits, or internal hyphens",
    ),
    IdentifierKind.stack_revision: _Shape(
        re.compile(rf"[0-9a-f]{{64}}|{STACK_REVISION_ABSENT}"),
        f"a lowercase 64-character SHA-256 hex digest or the literal "
        f"token {STACK_REVISION_ABSENT!r}",
    ),
}


class InvalidIdentifier(ValueError):
    """A value does not match the shape its identifier kind requires.

    The message names the field and the observed length but never the
    rejected value: rejected identifiers can carry caller-supplied content.
    """

    def __init__(self, kind: IdentifierKind, field: str, observed_length: int | None) -> None:
        self.kind = kind
        self.field = field
        self.observed_length = observed_length
        observed = (
            "a non-string value"
            if observed_length is None
            else f"a {observed_length}-character value"
        )
        super().__init__(f"{field} must be {_SHAPES[kind].description}; got {observed}.")


def is_identifier(kind: IdentifierKind, value: object) -> bool:
    """Report whether *value* is a string matching *kind*'s shape."""
    if not isinstance(value, str):
        return False
    return _SHAPES[kind].pattern.fullmatch(value) is not None


def validate_identifier(kind: IdentifierKind, value: object, *, field: str) -> str:
    """Return *value* when it matches *kind*, else raise ``InvalidIdentifier``."""
    if isinstance(value, str):
        if is_identifier(kind, value):
            return value
        raise InvalidIdentifier(kind, field, len(value))
    raise InvalidIdentifier(kind, field, None)
