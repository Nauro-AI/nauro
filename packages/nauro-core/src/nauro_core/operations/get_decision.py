"""``get_decision`` — return the body of a decision by number.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`GetDecisionResult`. Each transport's adapter wraps the call to
add transport-specific framing such as the ``store`` field;
the lookup itself is shared by construction.

``mode`` selects how much of the decision the ``content`` field carries:

* ``"full"`` (default) — the verbatim markdown body, unchanged.
* ``"header"`` — a compact projection (triage frontmatter fields, the
  title, and a short lede from the ``## Decision`` section) for cheap
  triage of the related-decision list a check returns. The projection
  rides in the existing ``content`` field; the result model shape is the
  same for both modes.
"""

from __future__ import annotations

from typing import Literal

from nauro_core.decision_model import Decision
from nauro_core.operations.decision_lookup import parse_decision_or_none
from nauro_core.operations.results import ErrorPayload, GetDecisionResult
from nauro_core.operations.store import Store
from nauro_core.parsing import _decision_filename, extract_decision_number

# Frontmatter fields the header projection carries, in render order. These
# are the triage signals a reader needs to decide whether a decision is
# worth a full read: where it sits in the supersession graph, when it was
# decided, and how it was classified.
_HEADER_FRONTMATTER_FIELDS: tuple[str, ...] = (
    "status",
    "supersedes",
    "superseded_by",
    "date",
    "decision_type",
    "confidence",
)

# Lede budget. The first paragraph of the ``## Decision`` section is the
# load-bearing sentence(s); cap it so the projection stays compact while
# still carrying enough rationale to triage on.
_LEDE_MAX_CHARS = 300


def get_decision(
    store: Store,
    number: int,
    mode: Literal["header", "full"] = "full",
) -> GetDecisionResult:
    """Return the decision matching ``number``, or a not-found error.

    Status filtering (active vs superseded) belongs to ``list_decisions``;
    ``get_decision`` resolves the exact number regardless of status so
    callers can still inspect the rationale of a superseded decision.

    Args:
        store: Storage adapter providing ``list_decisions`` / ``read_decision``.
        number: Decision number to resolve. Matched against the leading
            integer of each decision stem via
            :func:`nauro_core.parsing.extract_decision_number`.
        mode: ``"full"`` returns the verbatim markdown body; ``"header"``
            returns the compact triage projection.

    Returns:
        :class:`GetDecisionResult`. On a hit ``content`` holds the markdown
        body (``full``) or the projected block (``header``). On a miss
        ``error`` is populated with ``kind="error"`` and a reason that
        names the number.
    """
    for stem in store.list_decisions():
        parsed = extract_decision_number(stem)
        if parsed is not None and parsed == number:
            body = store.read_decision(stem)
            if body is not None:
                if mode == "header":
                    decision = parse_decision_or_none(body, _decision_filename(stem))
                    if decision is None:
                        return GetDecisionResult(
                            error=ErrorPayload(
                                kind="error",
                                reason=f"Decision {number} could not be parsed",
                            ),
                        )
                    return GetDecisionResult(content=_project_header(decision))
                return GetDecisionResult(content=body)
    return GetDecisionResult(
        error=ErrorPayload(kind="error", reason=f"Decision {number} not found"),
    )


def _project_header(decision: Decision) -> str:
    """Build the compact header projection for a parsed decision.

    Layout (blocks joined by a blank line):

        <ordered triage frontmatter lines>
        # NNN — Title
        <lede>            (omitted when the first ## Decision paragraph is empty)

    The empty-lede guard keeps supersession stubs and other bodies whose
    ``## Decision`` section opens with whitespace from emitting a dangling
    blank lede block.
    """
    frontmatter_lines: list[str] = []
    for field in _HEADER_FRONTMATTER_FIELDS:
        value = _frontmatter_value(decision, field)
        if value:
            frontmatter_lines.append(f"{field}: {value}")

    blocks: list[str] = ["\n".join(frontmatter_lines)]
    blocks.append(f"# {decision.num:03d} — {decision.title}")

    lede = _lede(decision.rationale)
    if lede:
        blocks.append(lede)

    return "\n\n".join(blocks)


def _frontmatter_value(decision, field: str) -> str:
    """Return the on-disk string for a triage frontmatter field, or ``""``.

    Enum-valued fields (``status``, ``decision_type``, ``confidence``)
    serialize to their underlying token; ``date`` to ISO format;
    supersession refs are already plain integer strings.
    """
    raw = getattr(decision, field, None)
    if raw is None:
        return ""
    if field == "date":
        return raw.isoformat()
    value = getattr(raw, "value", raw)
    return str(value)


def _lede(rationale: str) -> str:
    """Return the first paragraph of the rationale, truncated to the budget.

    Returns ``""`` when the rationale is empty or opens with whitespace
    only, so the projection omits the lede block entirely.
    """
    stripped = rationale.strip()
    if not stripped:
        return ""
    paragraph = stripped.split("\n\n", 1)[0].strip()
    if not paragraph:
        return ""
    if len(paragraph) <= _LEDE_MAX_CHARS:
        return paragraph
    return paragraph[: _LEDE_MAX_CHARS - 1].rstrip() + "…"
