"""``list_decisions`` — return decision summaries, sorted by number descending.

Cross-transport implementation: every transport adapter calls this
function with the same arguments and receives the same
:class:`ListDecisionsResult`. Each adapter wraps the call to add
transport-specific framing (``store`` field, telemetry emission); the
listing, filtering, and projection live here.

Status filtering belongs to this operation, not to ``get_decision``:
callers asking for "the active decision history" use the
``include_superseded=False`` default; ``get_decision`` always resolves
the body of a specific number regardless of status so superseded
rationale stays inspectable.
"""

from __future__ import annotations

from nauro_core.decision_model import DecisionStatus
from nauro_core.operations.decision_lookup import parse_all_decisions
from nauro_core.operations.results import DecisionSummary, ListDecisionsResult
from nauro_core.operations.store import Store


def list_decisions(
    store: Store,
    limit: int = 20,
    include_superseded: bool = False,
) -> ListDecisionsResult:
    """Return decision summaries sorted by number descending.

    Args:
        store: Storage adapter providing ``list_decisions`` / ``read_decision``.
        limit: Maximum number of rows to return.
        include_superseded: When ``False`` (default), drop rows whose
            status is ``superseded``; when ``True``, retain them.

    Returns:
        :class:`ListDecisionsResult` with ``decisions`` populated. Rows
        are sorted by decision number descending and sliced to ``limit``.
    """
    decisions = parse_all_decisions(store)

    if not include_superseded:
        decisions = [d for d in decisions if d.status is DecisionStatus.active]

    decisions.sort(key=lambda d: d.num, reverse=True)

    rows = [
        DecisionSummary(
            number=d.num,
            title=d.title,
            date=d.date.isoformat() if d.date else None,
            status=d.status.value,
            type=d.decision_type.value if d.decision_type else None,
            confidence=d.confidence.value,
        )
        for d in decisions[: max(limit, 0)]
    ]
    return ListDecisionsResult(decisions=rows)
