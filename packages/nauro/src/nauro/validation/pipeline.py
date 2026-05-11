"""Main validation pipeline — all writes to the store pass through here.

Pipeline: propose → validate (tier 1 → tier 2) → confirm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nauro.store.snapshot import capture_snapshot
from nauro.store.writer import append_decision, supersede_decision, update_decision
from nauro.validation.log import log_validation
from nauro.validation.pending import get_pending, remove_pending, store_pending
from nauro.validation.tier1 import screen_structural, update_hash_index
from nauro.validation.tier2 import check_similarity


@dataclass
class ValidationResult:
    status: str  # "confirmed", "pending_confirmation", "rejected"
    tier: int  # 1 or 2
    similar_decisions: list[dict] = field(default_factory=list)
    assessment: str = ""
    confirm_id: str | None = None
    operation: str = "add"  # "add", "update", "supersede", "reject"
    _decision_id: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "tier": self.tier,
            "similar_decisions": self.similar_decisions,
            "assessment": self.assessment,
            "confirm_id": self.confirm_id,
            "operation": self.operation,
        }


def validate_proposed_write(
    proposal: dict,
    project_path: Path,
    skip_validation: bool = False,
    operation: str = "add",
    affected_decision_id: str | None = None,
) -> ValidationResult:
    """Run the deterministic validation pipeline on a proposal.

    Args:
        proposal: Dict with title, rationale, rejected, confidence,
                  decision_type, reversibility, files_affected, source.
        project_path: Path to the project store.
        skip_validation: When True, skip Tier 2 and return a confirm_id after
                         Tier 1 passes.
        operation: "add" / "update" / "supersede" — caller's classification of
                   how this proposal relates to existing decisions.
        affected_decision_id: Required for "update" / "supersede"; the id of
                              the decision being updated or replaced.

    Returns:
        ValidationResult with status and details.
    """
    # --- Tier 1: Structural screening (always runs) ---
    action, reason = screen_structural(proposal, project_path)
    if action == "reject":
        result = ValidationResult(
            status="rejected",
            tier=1,
            operation="reject",
            assessment=reason or "Structural validation failed.",
        )
        log_validation(project_path, proposal, result.to_dict())
        return result

    # --- Skip Tier 2 when requested ---
    if skip_validation:
        pending_data = {
            "proposal": proposal,
            "operation": operation,
            "affected_decision_id": affected_decision_id,
        }
        confirm_id = store_pending(
            pending_data,
            {
                "tier": 1,
                "operation": operation,
                "similar_decisions": [],
                "assessment": "Validation skipped (skip_validation=true)."
                " Structural checks passed.",
            },
        )
        result = ValidationResult(
            status="pending_confirmation",
            tier=1,
            operation=operation,
            assessment="Validation skipped (skip_validation=true). Structural checks passed.",
            confirm_id=confirm_id,
        )
        log_validation(project_path, proposal, result.to_dict())
        return result

    # --- Tier 2: BM25 similarity ---
    t2_action, similar_decisions = check_similarity(proposal, project_path)

    if t2_action == "auto_confirm":
        # No similar decisions found — execute the caller-supplied operation.
        # Preserves supersede/update intent even when BM25 misses the affected
        # decision; the boundary already validated that affected_decision_id
        # resolves to a real file.
        decision_id, actual_operation = _execute_operation(
            operation, proposal, project_path, affected_decision_id
        )
        result = ValidationResult(
            status="confirmed",
            tier=2,
            operation=actual_operation,
            assessment="No similar existing decisions found.",
            similar_decisions=[],
        )
        result._decision_id = decision_id
        log_validation(project_path, proposal, result.to_dict())
        return result

    # --- Tier 2 found similar decisions — route through pending. ---
    pending_data = {
        "proposal": proposal,
        "operation": operation,
        "affected_decision_id": affected_decision_id,
    }
    confirm_id = store_pending(
        pending_data,
        {
            "tier": 2,
            "operation": operation,
            "similar_decisions": similar_decisions,
            "assessment": "Tier 2 found similar decisions; awaiting confirmation.",
        },
    )

    result = ValidationResult(
        status="pending_confirmation",
        tier=2,
        operation=operation,
        similar_decisions=similar_decisions,
        assessment="Tier 2 found similar decisions; awaiting confirmation.",
        confirm_id=confirm_id,
    )
    log_validation(project_path, proposal, result.to_dict())
    return result


def confirm_write(confirm_id: str, project_path: Path) -> dict:
    """Confirm a pending proposal and write it to the store."""
    pending = get_pending(confirm_id)
    if not pending:
        return {"error": "Invalid or expired confirm_id."}

    data = pending["proposal"]
    proposal = data["proposal"]
    operation = data["operation"]
    affected_decision_id = data["affected_decision_id"]

    decision_id, actual_operation = _execute_operation(
        operation, proposal, project_path, affected_decision_id
    )
    remove_pending(confirm_id)

    return {
        "status": "confirmed",
        "decision_id": decision_id,
        "title": proposal.get("title", ""),
        "operation": actual_operation,
    }


def _write_proposal(proposal: dict, project_path: Path) -> str:
    """Write a proposal as a new decision. Returns the decision ID."""
    rejected = proposal.get("rejected")
    rejected_alternatives = None
    if rejected:
        rejected_alternatives = []
        for item in rejected:
            if isinstance(item, dict):
                rejected_alternatives.append(item)
            elif isinstance(item, str):
                rejected_alternatives.append({"alternative": item, "reason": ""})

    path = append_decision(
        project_path,
        title=proposal.get("title", "Untitled"),
        rationale=proposal.get("rationale"),
        rejected=rejected_alternatives,
        confidence=proposal.get("confidence", "medium"),
        decision_type=proposal.get("decision_type"),
        reversibility=proposal.get("reversibility"),
        files_affected=proposal.get("files_affected"),
        source=proposal.get("source"),
    )

    decision_id = path.stem
    title = proposal.get("title", "")
    rationale = proposal.get("rationale", "")
    update_hash_index(title, rationale, decision_id, project_path)

    capture_snapshot(project_path, trigger=f"decision: {title}")
    return decision_id


def _execute_operation(
    operation: str,
    proposal: dict,
    project_path: Path,
    affected_decision_id: str | None,
) -> tuple[str, str]:
    """Execute the validated operation. Returns (decision_id, actual_operation).

    The actual operation may differ from the requested one if the boundary
    let through an update/supersede without affected_decision_id (it should
    not — see tool_propose_decision). The return value reflects what was
    actually written so callers can echo ground truth.
    """
    if operation == "supersede" and affected_decision_id:
        decision_id = supersede_decision(affected_decision_id, proposal, project_path)
        title = proposal.get("title", "")
        rationale = proposal.get("rationale", "")
        update_hash_index(title, rationale, decision_id, project_path)
        capture_snapshot(project_path, trigger=f"supersede {affected_decision_id}: {title}")
        return decision_id, "supersede"

    if operation == "update" and affected_decision_id:
        additional = proposal.get("rationale", "")
        decision_id = update_decision(affected_decision_id, additional, project_path)
        capture_snapshot(
            project_path,
            trigger=f"update {affected_decision_id}: {proposal.get('title', '')}",
        )
        return decision_id, "update"

    # "add", or fallback for update/supersede without affected_decision_id.
    return _write_proposal(proposal, project_path), "add"
