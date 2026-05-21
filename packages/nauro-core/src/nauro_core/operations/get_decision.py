"""``get_decision`` — return the full body of a decision by number.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`GetDecisionResult`. Each transport's adapter wraps the call to
add transport-specific framing (``store`` field, telemetry emission);
the lookup itself is shared by construction.
"""

from __future__ import annotations

from nauro_core.operations.results import ErrorPayload, GetDecisionResult
from nauro_core.operations.store import Store
from nauro_core.parsing import extract_decision_number


def get_decision(store: Store, number: int) -> GetDecisionResult:
    """Return the decision body matching ``number``, or a not-found error.

    Status filtering (active vs superseded) belongs to ``list_decisions``;
    ``get_decision`` resolves the exact number regardless of status so
    callers can still inspect the rationale of a superseded decision.

    Args:
        store: Storage adapter providing ``list_decisions`` / ``read_decision``.
        number: Decision number to resolve. Matched against the leading
            integer of each decision stem via
            :func:`nauro_core.parsing.extract_decision_number`.

    Returns:
        :class:`GetDecisionResult`. On a hit ``content`` holds the markdown
        body. On a miss ``error`` is populated with ``kind="error"`` and a
        reason that names the number.
    """
    for stem in store.list_decisions():
        parsed = extract_decision_number(stem)
        if parsed is not None and parsed == number:
            body = store.read_decision(stem)
            if body is not None:
                return GetDecisionResult(content=body)
    return GetDecisionResult(
        error=ErrorPayload(kind="error", reason=f"Decision {number} not found"),
    )
