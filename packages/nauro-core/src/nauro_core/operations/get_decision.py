"""``get_decision`` — return the body of a decision by number.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP all
call this function and receive the same :class:`GetDecisionResult`. Each
adapter adds transport-specific framing such as the ``store`` field.

``mode`` selects how much of the decision ``content`` carries:

* ``"full"`` (default) — the verbatim markdown body.
* ``"header"`` — a compact projection (triage frontmatter, title, and a short
  lede) for cheap standalone triage. It rides in the same ``content`` field, so
  the result shape is identical for both modes.
"""

from __future__ import annotations

from typing import Literal

from nauro_core.decision_model import Decision
from nauro_core.operations.decision_lookup import parse_decision_or_none
from nauro_core.operations.related_hits import decision_lede, frontmatter_value
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


def get_decision(
    store: Store,
    number: int,
    mode: Literal["header", "full"] = "full",
) -> GetDecisionResult:
    """Return the decision matching ``number``, or a not-found error. Status is not
    filtered, so a superseded decision resolves like any other. ``mode="header"``
    returns the compact triage projection instead of the verbatim body.
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
    """Build the compact header projection: ordered triage frontmatter lines, the
    ``# NNN — Title`` line, then the lede, joined by blank lines. The lede block is
    omitted when the first ``## Decision`` paragraph is empty.
    """
    frontmatter_lines: list[str] = []
    for field in _HEADER_FRONTMATTER_FIELDS:
        value = frontmatter_value(decision, field)
        if value:
            frontmatter_lines.append(f"{field}: {value}")

    blocks: list[str] = ["\n".join(frontmatter_lines)]
    blocks.append(f"# {decision.num:03d} — {decision.title}")

    lede = decision_lede(decision.rationale)
    if lede:
        blocks.append(lede)

    return "\n\n".join(blocks)
