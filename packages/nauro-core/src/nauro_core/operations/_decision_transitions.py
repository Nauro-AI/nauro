"""Pure decision and question transitions shared by local and hosted planning."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.questions import OpenQuestionsFile

_SLUG_MAX_LENGTH = 60


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
