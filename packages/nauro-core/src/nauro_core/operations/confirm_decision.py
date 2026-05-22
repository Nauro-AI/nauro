"""``confirm_decision`` — replay a pending proposal through the kernel writer.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same ``confirm_id`` and receive the same
:class:`ConfirmDecisionResult`. The kernel reads the same module-global
pending store that ``propose_decision`` writes into, dispatches the
pending entry to the private execute path, and removes the entry on both
the confirmed and the rejected branches so a second call with the same
id surfaces the unknown-id shape.

Length validation, envelope-token rejection, snapshot capture (skipped
intentionally on this path — the pre-cutover surface did not capture on
confirm), AGENTS.md regen, and the best-effort cloud push stay on the
adapter side per the locked Store Protocol boundary.
"""

from __future__ import annotations

from nauro_core.operations.propose_decision import (
    _execute_operation,
    _get_pending_store,
)
from nauro_core.operations.results import ConfirmDecisionResult, ErrorPayload
from nauro_core.operations.store import Store


def confirm_decision(store: Store, confirm_id: str) -> ConfirmDecisionResult:
    """Replay a pending proposal through the kernel's private execute path.

    Returns:
        :class:`ConfirmDecisionResult` with ``status`` of ``confirmed`` or
        ``rejected``. On the confirmed path ``decision_id``, ``operation``,
        ``touched_decisions``, ``title``, and ``resolved_questions`` are
        populated. On the rejected path ``error`` carries the structured
        payload — ``kind="rejected"`` for unknown/expired ids,
        ``kind="error"`` for half-state mid-sequence failures.
    """
    pending_store = _get_pending_store()
    pending = pending_store.get(confirm_id)
    if not pending:
        return ConfirmDecisionResult(
            status="rejected",
            operation="reject",
            error=ErrorPayload(
                kind="rejected",
                reason="Invalid or expired confirm_id.",
            ),
        )

    data = pending["proposal"]
    proposal = data["proposal"]
    operation = data["operation"]
    affected_decision_id = data["affected_decision_id"]

    decision_id, actual_operation, touched, resolved_ids, error = _execute_operation(
        store, operation, proposal, affected_decision_id
    )
    pending_store.remove(confirm_id)

    if error is not None:
        return ConfirmDecisionResult(
            status="rejected",
            operation=actual_operation,
            error=error,
            touched_decisions=list(touched),
        )

    return ConfirmDecisionResult(
        status="confirmed",
        operation=actual_operation,
        decision_id=decision_id,
        title=proposal.get("title", "") or None,
        touched_decisions=list(touched),
        resolved_questions=list(resolved_ids),
    )


__all__ = ["confirm_decision", "ConfirmDecisionResult"]
