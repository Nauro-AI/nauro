"""Pure decision and question transitions shared by local and hosted planning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionSource,
    DecisionStatus,
    DecisionType,
    RejectedAlternative,
    Reversibility,
)
from nauro_core.questions import OpenQuestionsFile

_SLUG_MAX_LENGTH = 60
_PROVENANCE_FIELDS = (
    "proposed_by",
    "approved_by",
    "approved_at",
    "proposal_id",
    "proposed_base_commit",
)


@dataclass(frozen=True)
class DecisionProvenance:
    proposed_by: str
    approved_by: str
    approved_at: str
    proposal_id: str
    proposed_base_commit: str | None = None


@dataclass(frozen=True)
class QuestionResolveTransition:
    content: str
    resolved_ids: tuple[str, ...] = ()
    relocated_ids: tuple[str, ...] = ()
    skipped_prose_ids: tuple[str, ...] = ()


def slugify_decision_title(title: str) -> str:
    """Return the existing canonical decision filename slug."""
    out_chars: list[str] = []
    prev_dash = False
    for char in title.lower():
        if char.isalnum():
            out_chars.append(char)
            prev_dash = False
        elif not prev_dash:
            out_chars.append("-")
            prev_dash = True
    slug = "".join(out_chars).strip("-")
    if len(slug) > _SLUG_MAX_LENGTH:
        truncated = slug[:_SLUG_MAX_LENGTH]
        trimmed = truncated.rsplit("-", 1)[0]
        slug = trimmed if len(trimmed) >= _SLUG_MAX_LENGTH // 2 else truncated
    return slug


def build_new_decision(
    *,
    number: int,
    decision_date: date,
    title: str,
    rationale: str,
    confidence: DecisionConfidence,
    decision_type: DecisionType | None,
    reversibility: Reversibility | None,
    source: DecisionSource | None,
    files_affected: Sequence[str],
    rejected: Sequence[RejectedAlternative],
    provenance: DecisionProvenance | None,
    extra_frontmatter: Mapping[str, object] | None = None,
) -> Decision:
    """Build a new current decision from explicit frozen inputs."""
    fields: dict[str, object] = dict(extra_frontmatter or {})
    if provenance is not None:
        fields.update(
            proposed_by=provenance.proposed_by,
            approved_by=provenance.approved_by,
            approved_at=provenance.approved_at,
            proposal_id=provenance.proposal_id,
            proposed_base_commit=provenance.proposed_base_commit,
        )
    return Decision(
        date=decision_date,
        version=1,
        status=DecisionStatus.active,
        confidence=confidence,
        decision_type=decision_type,
        reversibility=reversibility,
        source=source,
        files_affected=list(files_affected),
        rejected=list(rejected),
        num=number,
        title=title,
        rationale=rationale,
        **fields,
    )


def apply_current_version_provenance(
    decision: Decision,
    provenance: DecisionProvenance | None,
) -> Decision:
    """Apply one complete approval group, or clear a group for a new local version."""
    update = {field: None for field in _PROVENANCE_FIELDS}
    if provenance is not None:
        update.update(
            proposed_by=provenance.proposed_by,
            approved_by=provenance.approved_by,
            approved_at=provenance.approved_at,
            proposal_id=provenance.proposal_id,
            proposed_base_commit=provenance.proposed_base_commit,
        )
    return decision.model_copy(update=update)


def append_decision_update(
    decision: Decision,
    *,
    additional_rationale: str,
    update_date: date,
    provenance: DecisionProvenance | None,
) -> Decision:
    """Append one dated rationale version and replace current-version provenance."""
    version = decision.version + 1
    rationale = (
        f"{decision.rationale.strip()}\n\n"
        f"*Update (v{version}) — {update_date.isoformat()}:* {additional_rationale.strip()}"
    )
    updated = decision.model_copy(update={"version": version, "rationale": rationale})
    return apply_current_version_provenance(updated, provenance)


def attach_supersedes(decision: Decision, target_number: int) -> Decision:
    """Attach the derived backlink to the newly replacing decision."""
    return decision.model_copy(update={"supersedes": str(target_number)})


def mark_superseded(decision: Decision, replacement_number: int) -> Decision:
    """Flip a target while preserving every field of its last current version."""
    return decision.model_copy(
        update={
            "status": DecisionStatus.superseded,
            "superseded_by": str(replacement_number),
        }
    )


def resolve_questions_content(
    content: str,
    *,
    question_ids: Sequence[str],
    decision_number: int,
    resolved_date: date,
) -> QuestionResolveTransition:
    """Resolve and normalize questions from explicit content and date inputs."""
    if not question_ids:
        return QuestionResolveTransition(content=content)
    questions = OpenQuestionsFile.parse(content)
    resolved = questions.resolve(list(question_ids), decision_number, resolved_date)
    normalized = resolved.file.normalize()
    return QuestionResolveTransition(
        content=normalized.file.format(),
        resolved_ids=resolved.moved_ids,
        relocated_ids=normalized.relocated_ids,
        skipped_prose_ids=normalized.skipped_prose_ids,
    )
