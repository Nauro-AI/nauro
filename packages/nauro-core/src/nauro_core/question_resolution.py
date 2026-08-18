"""Deterministic question-resolution seam for local and hosted callers.

Callers capture exact decision and open-question bytes before invocation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from nauro_core.constants import OPEN_QUESTIONS_DEFAULT_BODY
from nauro_core.decision_model import DecisionStatus, parse_decision
from nauro_core.operations.results import ErrorPayload, FlagQuestionResult
from nauro_core.parsing import extract_decision_number
from nauro_core.questions import (
    EntryBlock,
    InvalidQuestionIdentifier,
    OpenQuestionsFile,
    format_question_id,
    parse_question_id,
)


@dataclass(frozen=True)
class ResolutionDecisionDocument:
    """An exact decision artifact captured by the caller."""

    stem: str
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", bytes(self.content))


@dataclass(frozen=True)
class QuestionResolutionOutcome:
    """The existing result envelope plus the exact effects of resolution."""

    result: FlagQuestionResult
    updated_open_questions_bytes: bytes | None
    resolved_question_ids: tuple[str, ...]


def resolve_question_document(
    *,
    open_questions_bytes: bytes | None,
    decision_documents: Sequence[ResolutionDecisionDocument],
    targets: Sequence[str],
    resolved_by: str,
    resolution_date: date,
) -> QuestionResolutionOutcome:
    """Resolve questions from immutable inputs without I/O or clock access."""
    source_bytes = open_questions_bytes
    if source_bytes is None:
        source_bytes = OPEN_QUESTIONS_DEFAULT_BODY.encode("utf-8")
    content = source_bytes.decode("utf-8")

    decision_num = extract_decision_number(resolved_by)
    if decision_num is None:
        return _rejected(
            f"resolved_by {resolved_by!r} is not a decision identifier "
            "(expected a form like D123, 123, or decision-123)."
        )

    matching = tuple(
        document
        for document in decision_documents
        if extract_decision_number(document.stem) == decision_num
    )
    if not matching:
        return _rejected(
            f"resolved_by names decision D{decision_num}, which does not exist "
            "in the captured decision set."
        )
    if len(matching) != 1:
        return _rejected(
            f"resolved_by names decision D{decision_num}, but the captured "
            "decision set contains duplicate-number artifacts."
        )

    document = matching[0]
    try:
        decision_body = document.content.decode("utf-8")
    except UnicodeDecodeError:
        return _rejected(f"Decision D{decision_num} is not valid UTF-8.")

    try:
        decision = parse_decision(decision_body, f"{document.stem}.md")
    except ValueError:
        return _rejected(f"Decision D{decision_num} is malformed.")

    if decision.num != decision_num:
        return _rejected(f"Decision artifact {document.stem!r} does not parse as D{decision_num}.")
    if decision.status is not DecisionStatus.active:
        return _rejected(f"Decision D{decision_num} is not active.")

    canonical_targets = _canonical_question_targets(targets)
    if not canonical_targets:
        return _rejected("resolved_by requires at least one id in targets to resolve.")

    parsed = OpenQuestionsFile.parse(content)
    ambiguous = parsed.ambiguous_ids
    requested_ambiguous = [target for target in canonical_targets if target in ambiguous]
    if requested_ambiguous:
        return _rejected(
            "targets contains ambiguous id(s) matching more than one entry: "
            + ", ".join(repr(target) for target in dict.fromkeys(requested_ambiguous))
            + ". Disambiguate before resolving."
        )

    entries_by_id: dict[str, EntryBlock] = {}
    for block in parsed.blocks:
        if isinstance(block, EntryBlock):
            entries_by_id.setdefault(block.entry.id, block)

    missing = [target for target in canonical_targets if target not in entries_by_id]
    if missing:
        return _rejected(
            "targets contains id(s) not present in open-questions.md: "
            + ", ".join(repr(target) for target in dict.fromkeys(missing))
            + "."
        )

    requested_unique = tuple(dict.fromkeys(canonical_targets))
    resolved_question_ids = tuple(
        target for target in requested_unique if entries_by_id[target].entry.resolved_by is None
    )
    resolved = parsed.resolve(
        ids=list(requested_unique),
        decision_num=decision_num,
        date=resolution_date,
    )
    normalized = resolved.file.normalize()
    updated_bytes = normalized.file.format().encode("utf-8")

    result = FlagQuestionResult(
        status="ok",
        num=None,
        relocated_ids=normalized.relocated_ids or None,
        skipped_prose_ids=normalized.skipped_prose_ids or None,
    )
    return QuestionResolutionOutcome(
        result=result,
        updated_open_questions_bytes=updated_bytes,
        resolved_question_ids=resolved_question_ids,
    )


def _canonical_question_targets(targets: Sequence[str]) -> list[str]:
    canonical: list[str] = []
    for target in targets:
        try:
            number = parse_question_id(target)
        except InvalidQuestionIdentifier:
            canonical.append(target)
        else:
            canonical.append(format_question_id(number))
    return canonical


def _rejected(reason: str) -> QuestionResolutionOutcome:
    return QuestionResolutionOutcome(
        result=FlagQuestionResult(
            status="rejected",
            error=ErrorPayload(kind="rejected", reason=reason),
        ),
        updated_open_questions_bytes=None,
        resolved_question_ids=(),
    )
