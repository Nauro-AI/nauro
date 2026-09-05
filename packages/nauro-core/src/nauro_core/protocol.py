"""Canonical wording for Nauro protocol claims used across instruction surfaces.

Six claims about Nauro's MCP tools recur in ``MCP_INSTRUCTIONS_STATIC``
(delivered via the MCP ``initialize.instructions`` field) and the
``/nauro-adopt`` skill body. This module owns the wording so the surfaces
cannot drift into paraphrase.

Surfaces compose either by importing the named fragment constants into
f-strings (as ``constants.py`` does), or by embedding ``<!-- protocol:NAME -->``
HTML-comment tokens in markdown templates that ``substitute_protocol_fragments``
resolves at load time. The comment shape is invisible in rendered markdown and
is interpreted by no templating engine in this repo.

Voice is impersonal so the same string reads naturally in 2nd-person MCP
instructions and 3rd-person skill bodies.
"""

from __future__ import annotations

CHECK_DECISION_RETURNS = (
    "`check_decision` returns related decisions via BM25 and a deterministic "
    "assessment. It does NOT judge conflicts."
)

GET_DECISION_BEFORE_PROPOSING = (
    "Related hits carry their triage headers inline; before proposing, call "
    "`get_decision` (`mode=full`) on each decision you reason about."
)

PROPOSE_DECISION_OPERATIONS = (
    "Pick the right `operation`:\n"
    "- `add` (default) - genuinely new ground.\n"
    "- `update` - rationale-only; needs `affected_decision_id`. The "
    "server rejects `title`, `confidence`, `decision_type`, `reversibility`, "
    "`files_affected`, and `rejected` - use supersede for those.\n"
    "- `supersede` - replace a decision this one contradicts or wholly "
    "subsumes; needs `affected_decision_id`."
)

UPDATE_SUPERSEDE_CARE = (
    "Default to `add` when uncertain - a later proposal can update or "
    "supersede it; a wrong supersede is hard to reverse."
)

NO_INVENT_RATIONALE = (
    "Do not invent rationale. Record only what was actually decided, with the "
    "reasoning that supports it."
)

_PROPOSAL_ADMISSION = (
    "Use `propose_decision` for a consequential future choice another task could "
    "get wrong without this judgment, or material evidence about an existing decision. "
    "State when it matters again and the consequence. Apply existing judgment without "
    "filing routine execution. Easy reversibility or code coverage does not disqualify "
    "a rule; honor explicit owner requests."
)

APPROVAL_BEFORE_PROPOSE = (
    "Present the complete add, update, or supersede draft as readable "
    "Markdown with related decisions, end the turn, and get explicit user "
    "approval from the user's next reply; the call commits immediately after "
    "validation."
)

_PROPOSAL_VISIBILITY_DETAIL = (
    "Never pair the draft with an approval prompt (AskUserQuestion) in the "
    "same turn - text before a tool call may never render. Prompt only once "
    "the draft is on screen from a prior turn. Arguments stay internal; show "
    "raw JSON only when debugging is requested."
)

RESOLVES_OPEN_QUESTIONS = (
    "When a proposal closes an open question, include its `[Q###]` id "
    "(legacy timestamp ids accepted) in `resolves_questions`. Named entries "
    "get a back-reference and, when prose-safe, move under `## Resolved`; "
    "unknown or ambiguous ids reject."
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
    """Resolve every ``<!-- protocol:NAME -->`` token in ``text``, single-pass.

    An unknown token is left intact so a typo surfaces instead of vanishing.
    """
    for token, value in _TOKENS.items():
        text = text.replace(token, value)
    return text


def protocol_tokens_in(text: str, *, only_unknown: bool = False) -> list[str]:
    """Return the fragment names of every ``<!-- protocol:NAME -->`` in ``text``.

    ``only_unknown=True`` keeps only names missing from :data:`CANONICAL_FRAGMENTS`.
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
    "APPROVAL_BEFORE_PROPOSE",
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
