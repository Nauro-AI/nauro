"""Main validation pipeline — all writes to the store pass through here.

Pipeline: propose → validate (tier 1 → tier 2) → confirm.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from nauro_core import extract_decision_number
from nauro_core.constants import MIN_RATIONALE_LENGTH, OPEN_QUESTIONS_MD
from nauro_core.questions import OpenQuestionsFile

from nauro.store.snapshot import capture_snapshot
from nauro.store.writer import (
    append_decision,
    resolve_questions_in_file,
    supersede_decision,
    update_decision,
)
from nauro.validation.log import log_validation
from nauro.validation.pending import get_pending, remove_pending, store_pending
from nauro.validation.tier1 import screen_structural, update_hash_index
from nauro.validation.tier2 import check_similarity

logger = logging.getLogger("nauro.validation.pipeline")

# D133: operation="update" appends rationale only — update_decision reads only
# affected_decision_id + rationale. Any value in these fields would be silently
# dropped on local stdio (and rejected on remote MCP); reject loudly at the
# boundary so the wording in PROPOSE_DECISION_OPERATIONS holds on both
# transports. Order mirrors mcp-server/src/mcp_server/validation.py:261-268.
_UPDATE_DISALLOWED_FIELDS: tuple[str, ...] = (
    "title",
    "rejected",
    "files_affected",
    "decision_type",
    "reversibility",
    "confidence",
)


@dataclass
class ValidationResult:
    status: str  # "confirmed", "pending_confirmation", "rejected"
    tier: int  # 1 or 2
    similar_decisions: list[dict] = field(default_factory=list)
    assessment: str = ""
    confirm_id: str | None = None
    operation: str = "add"  # "add", "update", "supersede", "reject"
    resolved_questions: tuple[str, ...] = ()
    _decision_id: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "tier": self.tier,
            "similar_decisions": self.similar_decisions,
            "assessment": self.assessment,
            "confirm_id": self.confirm_id,
            "operation": self.operation,
            "resolved_questions": list(self.resolved_questions),
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
    # --- D133: reject metadata fields on operation="update" ---
    # update_decision writes only the new rationale onto the existing target;
    # any value in the disallowed fields would be silently dropped. Reject
    # loudly and point the caller at supersede. Sits before Tier 1 so the
    # rejection assessment names the offending fields rather than failing as
    # a generic structural error.
    if operation == "update":
        disallowed = [
            name
            for name in _UPDATE_DISALLOWED_FIELDS
            if proposal.get(name)
            and (not isinstance(proposal.get(name), str) or proposal.get(name).strip())
        ]
        if disallowed:
            result = ValidationResult(
                status="rejected",
                tier=0,
                operation="update",
                assessment=(
                    'operation="update" appends rationale only; cannot change '
                    f"{', '.join(disallowed)}. "
                    'Use operation="supersede" to replace the decision with new metadata.'
                ),
            )
            log_validation(project_path, proposal, result.to_dict())
            return result

    # --- D139: reject unknown resolves_questions ids at the boundary ---
    # Validate every supplied id exists in open-questions.md (either open
    # or already-resolved). Unknown ids reject before Tier 1 so the
    # assessment names the offending ids rather than slipping past the
    # gate and failing on the write side.
    requested_resolves = list(proposal.get("resolves_questions") or [])
    if requested_resolves:
        unknown = _unknown_question_ids(requested_resolves, project_path)
        if unknown:
            result = ValidationResult(
                status="rejected",
                tier=0,
                operation=operation,
                assessment=(
                    "resolves_questions contains unknown timestamp id(s): "
                    + ", ".join(repr(x) for x in unknown)
                    + ". Call get_context (L0 lists every open question) to "
                    "see the canonical ids in open-questions.md."
                ),
            )
            log_validation(project_path, proposal, result.to_dict())
            return result

    # --- Tier 1: Structural screening ---
    # - update: rationale-only (length + minimum). The existing target supplies
    #   the title, so the standard "title empty" reject would otherwise prevent
    #   title="" from being the legitimate rationale-only signal (per D133).
    # - add / supersede: full structural screen.
    if operation == "update":
        rationale = (proposal.get("rationale") or "").strip()
        if not rationale:
            action, reason = "reject", "Rationale is empty."
        elif len(rationale) < MIN_RATIONALE_LENGTH:
            action, reason = (
                "reject",
                f"Rationale too short ({len(rationale)} chars). Minimum {MIN_RATIONALE_LENGTH}.",
            )
        else:
            action, reason = "pass", None
    else:
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
        decision_id, actual_operation, resolved_ids = _execute_operation(
            operation, proposal, project_path, affected_decision_id
        )
        result = ValidationResult(
            status="confirmed",
            tier=2,
            operation=actual_operation,
            assessment="No similar existing decisions found.",
            similar_decisions=[],
            resolved_questions=resolved_ids,
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

    decision_id, actual_operation, resolved_ids = _execute_operation(
        operation, proposal, project_path, affected_decision_id
    )
    remove_pending(confirm_id)

    response: dict = {
        "status": "confirmed",
        "decision_id": decision_id,
        "title": proposal.get("title", ""),
        "operation": actual_operation,
    }
    if resolved_ids:
        response["resolved_questions"] = list(resolved_ids)
    return response


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
) -> tuple[str, str, tuple[str, ...]]:
    """Execute the validated operation.

    Returns ``(decision_id, actual_operation, resolved_question_ids)``. The
    actual operation may differ from the requested one if the boundary let
    through an update/supersede without affected_decision_id (it should not
    — see tool_propose_decision). The return value reflects what was
    actually written so callers can echo ground truth.

    Question resolution (D139) is applied best-effort after the decision
    write — see :func:`_apply_question_resolves` for the failure posture.
    """
    if operation == "supersede" and affected_decision_id:
        decision_id = supersede_decision(affected_decision_id, proposal, project_path)
        title = proposal.get("title", "")
        rationale = proposal.get("rationale", "")
        update_hash_index(title, rationale, decision_id, project_path)
        capture_snapshot(project_path, trigger=f"supersede {affected_decision_id}: {title}")
        resolved = _apply_question_resolves(proposal, project_path, decision_id)
        return decision_id, "supersede", resolved

    if operation == "update" and affected_decision_id:
        additional = proposal.get("rationale", "")
        decision_id = update_decision(affected_decision_id, additional, project_path)
        capture_snapshot(
            project_path,
            trigger=f"update {affected_decision_id}: {proposal.get('title', '')}",
        )
        resolved = _apply_question_resolves(proposal, project_path, decision_id)
        return decision_id, "update", resolved

    # "add", or fallback for update/supersede without affected_decision_id.
    decision_id = _write_proposal(proposal, project_path)
    resolved = _apply_question_resolves(proposal, project_path, decision_id)
    return decision_id, "add", resolved


def _apply_question_resolves(
    proposal: dict, project_path: Path, decision_id: str
) -> tuple[str, ...]:
    """Apply ``resolves_questions`` to open-questions.md after a successful
    decision write.

    Best-effort: the boundary already rejected unknown ids, so this can only
    fail on file I/O. Failures are logged with ``unresolved_ids`` and the
    decision write stands — D132's posture for partial writes.
    """
    ids = list(proposal.get("resolves_questions") or [])
    if not ids:
        return ()
    num = extract_decision_number(decision_id)
    if num is None:
        return ()
    try:
        result = resolve_questions_in_file(
            project_path,
            ids,
            num,
            datetime.now(timezone.utc).date(),
        )
    except Exception:
        logger.exception(
            "question-resolution failed after decision %s wrote; "
            "decision stands. unresolved_ids=%s",
            decision_id,
            ids,
        )
        return ()
    if result.moved_ids:
        capture_snapshot(
            project_path,
            trigger=f"resolved questions via {decision_id}",
        )
    return result.moved_ids


def _unknown_question_ids(ids: list[str], project_path: Path) -> list[str]:
    """Return any caller-supplied ids absent from open-questions.md."""
    oq_path = project_path / OPEN_QUESTIONS_MD
    if not oq_path.exists():
        return list(ids)
    file = OpenQuestionsFile.parse(oq_path.read_text())
    known = set(file.open_ids) | set(file.resolved_ids)
    return [tid for tid in ids if tid not in known]
