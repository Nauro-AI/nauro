"""Main validation pipeline — all writes to the store pass through here.

Pipeline: propose → validate (tier 1 → tier 2 → tier 3) → confirm.
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
from nauro.validation.tier3 import evaluate_with_llm


@dataclass
class ValidationResult:
    status: str  # "confirmed", "pending_confirmation", "rejected", "noop"
    tier: int  # 1, 2, or 3
    similar_decisions: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    assessment: str = ""
    suggested_refinements: str | None = None
    confirm_id: str | None = None
    operation: str = "add"  # "add", "update", "supersede", "noop", "reject"
    _decision_id: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "tier": self.tier,
            "similar_decisions": self.similar_decisions,
            "conflicts": self.conflicts,
            "assessment": self.assessment,
            "suggested_refinements": self.suggested_refinements,
            "confirm_id": self.confirm_id,
            "operation": self.operation,
        }


def validate_proposed_write(
    proposal: dict,
    project_path: Path,
    auto_confirm: bool = False,
    api_key: str | None = None,
    skip_validation: bool = False,
) -> ValidationResult:
    """Run the tiered validation pipeline on a proposal.

    Args:
        proposal: Dict with title, rationale, rejected, confidence,
                  decision_type, reversibility, files_affected, source.
        project_path: Path to the project store.
        auto_confirm: True for extraction pipeline (skip confirm step),
                      False for MCP tools (require explicit confirmation).
        api_key: Anthropic API key for Tier 3 LLM calls. Falls back to
                 ANTHROPIC_API_KEY env var.
        skip_validation: When True, skip tier-2/tier-3 and return a
                         confirm_id after tier-1 passes.

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

    # --- Skip tier-2/tier-3 when requested ---
    if skip_validation:
        pending_data = {
            "proposal": proposal,
            "operation": "add",
            "llm_result": {},
        }
        confirm_id = store_pending(
            pending_data,
            {
                "tier": 1,
                "operation": "add",
                "similar_decisions": [],
                "conflicts": [],
                "assessment": "Validation skipped (skip_validation=true)."
                " Structural checks passed.",
            },
        )
        result = ValidationResult(
            status="pending_confirmation",
            tier=1,
            operation="add",
            assessment="Validation skipped (skip_validation=true). Structural checks passed.",
            confirm_id=confirm_id,
        )
        log_validation(project_path, proposal, result.to_dict())
        return result

    # --- Tier 2: Embedding similarity ---
    t2_action, similar_decisions = check_similarity(proposal, project_path)

    if t2_action == "auto_confirm":
        # No similar decisions — auto-confirm
        if auto_confirm:
            decision_id = _write_proposal(proposal, project_path)
            result = ValidationResult(
                status="confirmed",
                tier=2,
                operation="add",
                assessment="New decision — no similar existing decisions found.",
                similar_decisions=[],
            )
            result._decision_id = decision_id
            log_validation(project_path, proposal, result.to_dict())
            return result
        else:
            # MCP path: auto-confirm without pending if no similar decisions
            decision_id = _write_proposal(proposal, project_path)
            result = ValidationResult(
                status="confirmed",
                tier=2,
                operation="add",
                assessment="New decision — no similar existing decisions found.",
                similar_decisions=[],
            )
            result._decision_id = decision_id
            log_validation(project_path, proposal, result.to_dict())
            return result

    # --- Tier 3: LLM evaluation (only when similarity detected) ---
    llm_result = evaluate_with_llm(proposal, similar_decisions, project_path, api_key=api_key)
    operation = llm_result.get("operation", "add")

    if operation == "noop":
        result = ValidationResult(
            status="noop",
            tier=3,
            operation="noop",
            similar_decisions=similar_decisions,
            conflicts=llm_result.get("conflicts", []),
            assessment=llm_result.get("assessment", "Redundant — already captured."),
            suggested_refinements=llm_result.get("suggested_refinements"),
        )
        log_validation(project_path, proposal, result.to_dict())
        return result

    if operation == "hold":
        # LLM was unavailable. For extraction (auto_confirm), skip the write so
        # we don't blindly add decisions that may be duplicates or conflicts.
        # For the MCP path, fall through to pending so a human can confirm.
        if auto_confirm:
            result = ValidationResult(
                status="held",
                tier=3,
                operation="hold",
                similar_decisions=similar_decisions,
                assessment=llm_result.get("assessment", "LLM evaluation unavailable."),
            )
            log_validation(project_path, proposal, result.to_dict())
            return result
        # MCP path: fall through to pending confirmation below

    if auto_confirm and operation in ("add", "supersede", "update"):
        decision_id = _execute_operation(operation, proposal, project_path, llm_result)
        result = ValidationResult(
            status="confirmed",
            tier=3,
            operation=operation,
            similar_decisions=similar_decisions,
            conflicts=llm_result.get("conflicts", []),
            assessment=llm_result.get("assessment", ""),
            suggested_refinements=llm_result.get("suggested_refinements"),
        )
        result._decision_id = decision_id
        log_validation(project_path, proposal, result.to_dict())
        return result

    # MCP path: return pending confirmation
    pending_data = {
        "proposal": proposal,
        "operation": operation,
        "llm_result": llm_result,
    }
    confirm_id = store_pending(
        pending_data,
        {
            "tier": 3,
            "operation": operation,
            "similar_decisions": similar_decisions,
            "conflicts": llm_result.get("conflicts", []),
            "assessment": llm_result.get("assessment", ""),
            "suggested_refinements": llm_result.get("suggested_refinements"),
        },
    )

    result = ValidationResult(
        status="pending_confirmation",
        tier=3,
        operation=operation,
        similar_decisions=similar_decisions,
        conflicts=llm_result.get("conflicts", []),
        assessment=llm_result.get("assessment", ""),
        suggested_refinements=llm_result.get("suggested_refinements"),
        confirm_id=confirm_id,
    )
    log_validation(project_path, proposal, result.to_dict())
    return result


def confirm_write(confirm_id: str, project_path: Path) -> dict:
    """Confirm a pending proposal and write it to the store.

    Returns:
        Dict with decision metadata (id, title, path) or error info.
    """
    pending = get_pending(confirm_id)
    if not pending:
        return {"error": "Invalid or expired confirm_id."}

    data = pending["proposal"]
    proposal = data["proposal"]
    operation = data["operation"]
    llm_result = data["llm_result"]

    decision_id = _execute_operation(operation, proposal, project_path, llm_result)
    remove_pending(confirm_id)

    return {
        "status": "confirmed",
        "decision_id": decision_id,
        "title": proposal.get("title", ""),
        "operation": operation,
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

    # Update indexes
    decision_id = path.stem
    title = proposal.get("title", "")
    rationale = proposal.get("rationale", "")
    update_hash_index(title, rationale, decision_id, project_path)

    capture_snapshot(project_path, trigger=f"decision: {title}")
    return decision_id


def _execute_operation(operation: str, proposal: dict, project_path: Path, llm_result: dict) -> str:
    """Execute the validated operation (add, update, supersede)."""
    if operation == "supersede":
        old_id = llm_result.get("affected_decision_id")
        if old_id:
            decision_id = supersede_decision(old_id, proposal, project_path)
            title = proposal.get("title", "")
            rationale = proposal.get("rationale", "")
            update_hash_index(title, rationale, decision_id, project_path)
            capture_snapshot(project_path, trigger=f"supersede {old_id}: {title}")
            return decision_id
        # Fall through to add if no affected_decision_id
        return _write_proposal(proposal, project_path)

    elif operation == "update":
        target_id = llm_result.get("affected_decision_id")
        if target_id:
            additional = proposal.get("rationale", "")
            decision_id = update_decision(target_id, additional, project_path)
            capture_snapshot(
                project_path,
                trigger=f"update {target_id}: {proposal.get('title', '')}",
            )
            return decision_id
        return _write_proposal(proposal, project_path)

    else:  # "add"
        return _write_proposal(proposal, project_path)
