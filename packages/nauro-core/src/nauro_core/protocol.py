"""Canonical wording for Nauro protocol claims used across instruction surfaces.

Five claims about Nauro's MCP tools recur in multiple surfaces:
``MCP_INSTRUCTIONS_STATIC`` (delivered via the MCP ``initialize.instructions``
field) and the ``/nauro-adopt`` skill body. Restating them in each surface
produced paraphrase-level drift that gave agents slightly different rules
depending on which surface they read.

This module owns the canonical wording. Surfaces compose by either:

* Importing the named fragment constants (e.g. ``CHECK_DECISION_RETURNS``) and
  splicing them into Python f-strings. Used by ``constants.py`` for
  ``MCP_INSTRUCTIONS_STATIC``.
* Embedding ``<!-- protocol:NAME -->`` HTML-comment tokens in markdown source
  templates. ``substitute_protocol_fragments`` resolves the tokens at load time
  in ``nauro.skills``. The HTML-comment shape is invisible in rendered markdown
  and not interpreted by any templating engine in this repo (which is
  deliberately f-string-only — see CLAUDE.md).

Voice is impersonal so the same string reads naturally in both 2nd-person MCP
instruction framing and 3rd-person skill-body framing.
"""

from __future__ import annotations

CHECK_DECISION_RETURNS = (
    "`check_decision` returns related decisions via BM25 retrieval and a "
    "deterministic assessment. It does NOT judge conflicts."
)

GET_DECISION_BEFORE_PROPOSING = (
    "When the response lists related decisions, call `get_decision` on each "
    "before proposing — `mode=header` to triage, `mode=full` for those you "
    "reason about; the assessment doesn't judge."
)

PROPOSE_DECISION_OPERATIONS = (
    "Pick the right `operation`:\n"
    "- `add` (default) — genuinely new ground; no existing decision is being changed.\n"
    "- `update` — rationale-only; provide `affected_decision_id`. The "
    "server rejects `title`, `confidence`, `decision_type`, `reversibility`, "
    "`files_affected`, and `rejected` at the boundary — use supersede for any "
    "of those.\n"
    "- `supersede` — replace an existing decision with one that contradicts or "
    "wholly subsumes it. Provide `affected_decision_id`."
)

UPDATE_SUPERSEDE_CARE = (
    "Default to `add` when uncertain — a later proposal can update or "
    "supersede it once context clarifies. A wrongly-confirmed supersede is "
    "hard to reverse."
)

NO_INVENT_RATIONALE = (
    "Do not invent rationale. Record only what was actually decided, with the "
    "reasoning that supports it."
)

RESOLVES_OPEN_QUESTIONS = (
    "When a proposal closes one of `get_context`'s open questions, include "
    "the question's `[Q###]` id in `resolves_questions`. Legacy "
    "`[YYYY-MM-DD HH:MM UTC]` ids are still accepted for entries that "
    "predate the Q-form rollout. The named entries move under "
    "`## Resolved` with a back-reference to the new decision on confirm; "
    "unknown or ambiguous ids reject at the boundary."
)

CANONICAL_FRAGMENTS: dict[str, str] = {
    "CHECK_DECISION_RETURNS": CHECK_DECISION_RETURNS,
    "GET_DECISION_BEFORE_PROPOSING": GET_DECISION_BEFORE_PROPOSING,
    "PROPOSE_DECISION_OPERATIONS": PROPOSE_DECISION_OPERATIONS,
    "UPDATE_SUPERSEDE_CARE": UPDATE_SUPERSEDE_CARE,
    "NO_INVENT_RATIONALE": NO_INVENT_RATIONALE,
    "RESOLVES_OPEN_QUESTIONS": RESOLVES_OPEN_QUESTIONS,
}

_TOKEN_PREFIX = "<!-- protocol:"
_TOKEN_SUFFIX = " -->"

_TOKENS: dict[str, str] = {
    f"{_TOKEN_PREFIX}{name}{_TOKEN_SUFFIX}": value for name, value in CANONICAL_FRAGMENTS.items()
}

# Self-check at import time: no fragment may itself contain the token prefix,
# or a single substitution pass would re-trigger and chain. ``raise`` rather
# than ``assert`` so the invariant survives ``python -O`` (which strips
# asserts) — module-load self-checks must always run.
for _name, _value in CANONICAL_FRAGMENTS.items():
    if _TOKEN_PREFIX in _value:
        raise ValueError(
            f"fragment {_name!r} contains a protocol token prefix, which "
            "would chain on substitution"
        )


def substitute_protocol_fragments(text: str) -> str:
    """Resolve every ``<!-- protocol:NAME -->`` token in ``text``.

    Single-pass: each known token is replaced exactly once per occurrence.
    Unknown tokens (``<!-- protocol:NOT_A_FRAGMENT -->``) are left intact so a
    typo surfaces via :func:`protocol_tokens_in` rather than silently vanishing.
    """
    for token, value in _TOKENS.items():
        text = text.replace(token, value)
    return text


def protocol_tokens_in(text: str, *, only_unknown: bool = False) -> list[str]:
    """Return the fragment names of every ``<!-- protocol:NAME -->`` in ``text``.

    With ``only_unknown=True``, returns names that are not registered in
    :data:`CANONICAL_FRAGMENTS` — typos surface against the source file
    rather than against the rendered output.
    """
    names: list[str] = []
    cursor = 0
    while True:
        start = text.find(_TOKEN_PREFIX, cursor)
        if start < 0:
            break
        name_start = start + len(_TOKEN_PREFIX)
        end = text.find(_TOKEN_SUFFIX, name_start)
        if end < 0:
            break
        name = text[name_start:end]
        if only_unknown and name in CANONICAL_FRAGMENTS:
            cursor = end + len(_TOKEN_SUFFIX)
            continue
        names.append(name)
        cursor = end + len(_TOKEN_SUFFIX)
    return names


__all__ = [
    "CANONICAL_FRAGMENTS",
    "CHECK_DECISION_RETURNS",
    "GET_DECISION_BEFORE_PROPOSING",
    "NO_INVENT_RATIONALE",
    "PROPOSE_DECISION_OPERATIONS",
    "RESOLVES_OPEN_QUESTIONS",
    "UPDATE_SUPERSEDE_CARE",
    "protocol_tokens_in",
    "substitute_protocol_fragments",
]
