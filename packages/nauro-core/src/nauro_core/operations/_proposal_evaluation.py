"""Pure proposal validation and similarity evaluation over parsed inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nauro_core.constants import MIN_RATIONALE_LENGTH
from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.operations.related_hits import to_related_decisions
from nauro_core.operations.results import ProposeDecisionResult, RelatedDecision
from nauro_core.questions import EntryBlock, OpenQuestionsFile
from nauro_core.validation import check_bm25_similarity, screen_structural

_UPDATE_DISALLOWED_FIELDS: tuple[str, ...] = (
    "title",
    "rejected",
    "files_affected",
    "decision_type",
    "reversibility",
    "confidence",
)


@dataclass(frozen=True)
class ProposalEvaluation:
    similar_decisions: tuple[RelatedDecision, ...]
    assessment: str


def validate_proposal_request(
    proposal: dict,
    *,
    operation: Literal["add", "update", "supersede"],
    questions_file: OpenQuestionsFile | None,
) -> ProposeDecisionResult | None:
    """Validate request fields that do not require the decision corpus."""
    metadata_rejection = validate_update_metadata(proposal, operation=operation)
    if metadata_rejection is not None:
        return metadata_rejection
    question_rejection = validate_question_resolves(
        proposal,
        operation=operation,
        questions_file=questions_file,
    )
    if question_rejection is not None:
        return question_rejection
    return validate_update_rationale(proposal, operation=operation)


def validate_update_metadata(
    proposal: dict,
    *,
    operation: Literal["add", "update", "supersede"],
) -> ProposeDecisionResult | None:
    """Reject metadata that rationale-only update cannot consume."""
    if operation == "update":
        disallowed = [
            name
            for name in _UPDATE_DISALLOWED_FIELDS
            if proposal.get(name)
            and (not isinstance(proposal.get(name), str) or proposal.get(name).strip())
        ]
        if disallowed:
            return ProposeDecisionResult(
                status="rejected",
                tier=0,
                operation="update",
                assessment=(
                    'operation="update" appends rationale only; cannot change '
                    f"{', '.join(disallowed)}. "
                    'Use operation="supersede" to replace the decision with new metadata.'
                ),
            )
    return None


def validate_question_resolves(
    proposal: dict,
    *,
    operation: Literal["add", "update", "supersede"],
    questions_file: OpenQuestionsFile | None,
) -> ProposeDecisionResult | None:
    """Validate question identifiers against one parsed questions file."""
    requested = list(proposal.get("resolves_questions") or [])
    if requested:
        unknown = _unknown_question_ids(requested, questions_file)
        if unknown:
            return ProposeDecisionResult(
                status="rejected",
                tier=0,
                operation=operation,
                assessment=(
                    "resolves_questions contains unknown id(s): "
                    + ", ".join(repr(value) for value in unknown)
                    + ". Call get_context (L0 lists every open question) to "
                    "see the canonical ids in open-questions.md."
                ),
            )
        ambiguous = _ambiguous_question_ids(requested, questions_file)
        if ambiguous:
            offenders = "; ".join(
                f"{requested_id!r} matches {len(counterparts)} entries — disambiguate "
                f"to one of: {', '.join(counterparts)}"
                for requested_id, counterparts in ambiguous.items()
            )
            return ProposeDecisionResult(
                status="rejected",
                tier=0,
                operation=operation,
                assessment="resolves_questions contains ambiguous id(s): " + offenders + ".",
            )
    return None


def validate_update_rationale(
    proposal: dict,
    *,
    operation: Literal["add", "update", "supersede"],
) -> ProposeDecisionResult | None:
    """Apply the update-specific Tier 1 rationale check without corpus reads."""
    if operation != "update":
        return None
    rationale = (proposal.get("rationale") or "").strip()
    if not rationale:
        reason = "Rationale is empty."
    elif len(rationale) < MIN_RATIONALE_LENGTH:
        reason = f"Rationale too short ({len(rationale)} chars). Minimum {MIN_RATIONALE_LENGTH}."
    else:
        return None
    return ProposeDecisionResult(
        status="rejected",
        tier=1,
        operation="reject",
        assessment=reason,
    )


def evaluate_parsed_proposal(
    proposal: dict,
    *,
    operation: Literal["add", "update", "supersede"],
    decisions: list[Decision],
    existing_hashes: set[str],
    affected_number: int | None,
    enforce_claim_conflicts: bool,
) -> ProposalEvaluation | ProposeDecisionResult:
    """Evaluate structure and similarity over one immutable parsed corpus."""
    if operation != "update":
        active = (
            [
                decision
                for decision in decisions
                if decision.status is DecisionStatus.active and decision.num != affected_number
            ]
            if enforce_claim_conflicts
            else []
        )
        action, reason = screen_structural(
            proposal,
            existing_hashes if enforce_claim_conflicts else set(),
            active,
        )
        if action == "reject":
            return ProposeDecisionResult(
                status="rejected",
                tier=1,
                operation="reject",
                assessment=reason or "Structural validation failed.",
            )

    _action, similar_raw = check_bm25_similarity(proposal, decisions)
    similar = tuple(to_related_decisions(similar_raw, decisions))
    assessment = (
        "Tier 2 surfaced similar decisions; review them and confirm with the "
        "user before proposing further related writes."
        if similar
        else "No similar existing decisions found."
    )
    return ProposalEvaluation(similar_decisions=similar, assessment=assessment)


def _unknown_question_ids(
    ids: list[str],
    questions_file: OpenQuestionsFile | None,
) -> list[str]:
    if questions_file is None:
        return list(ids)
    known = questions_file.known_question_ids
    return [value for value in ids if value not in known]


def _ambiguous_question_ids(
    ids: list[str],
    questions_file: OpenQuestionsFile | None,
) -> dict[str, list[str]]:
    if questions_file is None:
        return {}
    collisions = questions_file.ambiguous_ids
    requested = [value for value in ids if value in collisions]
    if not requested:
        return {}
    counterparts: dict[str, list[str]] = {value: [] for value in requested}
    for block in questions_file.blocks:
        if not isinstance(block, EntryBlock):
            continue
        entry_id = block.entry.id
        if entry_id in counterparts:
            counterparts[entry_id].append(
                f"Q{block.entry.num}" if block.entry.num is not None else "<no-Q-id>"
            )
    return counterparts
