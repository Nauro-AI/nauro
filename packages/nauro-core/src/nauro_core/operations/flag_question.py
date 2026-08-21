"""``flag_question`` — append an open question, or resolve existing ones.

CLI, local stdio MCP, and remote HTTP MCP all call this function and receive
the same :class:`FlagQuestionResult`. The kernel owns parse, scan, mint and
insert through the :class:`Store` protocol; length validation, envelope-token
rejection, similarity hinting, snapshot capture, and cloud push are adapter-side.

``resolved_by`` discriminates the two actions. Append mints a fresh ``Q###``
and inserts it right after the top-level ``# `` header, and short-circuits with
a rejection when a named target is already resolved. Resolve stamps the
``targets`` entries, then runs ``normalize`` so prose-safe stamped entries move
below ``## Resolved``; ``relocated_ids`` and ``skipped_prose_ids`` name what
moved and what a detached body paragraph held back. Re-resolving is idempotent.

Freshness is bounded by the working copy the Store sees.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nauro_core.constants import OPEN_QUESTIONS_DEFAULT_BODY, OPEN_QUESTIONS_MD
from nauro_core.operations.results import ErrorPayload, FlagQuestionResult
from nauro_core.operations.store import Store
from nauro_core.question_append import (
    allocate_question_number,
    compose_question_entry,
    insert_question_entry,
)
from nauro_core.question_resolution import (
    ResolutionDecisionDocument,
    _canonical_question_targets,
    resolve_question_document,
)
from nauro_core.questions import (
    EntryBlock,
    OpenQuestionsFile,
)


def flag_question(
    store: Store,
    question: str | None = None,
    context: str | None = None,
    targets: list[str] | None = None,
    resolved_by: str | None = None,
) -> FlagQuestionResult:
    """Append ``question``, minting ``num`` as the next ``Q###``, or with ``resolved_by`` set
    resolve the ``targets`` entries; ``context`` is discarded. Rejects write nothing: a resolved
    or ambiguous append target, a missing or inactive decision, or a missing resolve target.
    """
    del context  # adapter composes context into question; kernel sees one body.
    canonical_targets = _canonical_question_targets(targets or [])

    has_question = question is not None and question.strip() != ""
    if resolved_by is not None:
        if has_question:
            return _reject(
                "Pass either question (to append a new flag) or resolved_by "
                "(to resolve existing entries), not both."
            )
        return _resolve(store, canonical_targets, resolved_by)

    if not has_question:
        return _reject(
            "Pass either question (to append a new flag) or resolved_by "
            "(to resolve existing entries)."
        )

    assert question is not None  # narrowed by has_question above.
    content = store.read_file(OPEN_QUESTIONS_MD) or OPEN_QUESTIONS_DEFAULT_BODY
    parsed = OpenQuestionsFile.parse(content)

    if canonical_targets:
        rejection = _short_circuit_if_resolved(parsed, canonical_targets)
        if rejection is not None:
            return rejection

    next_num = allocate_question_number(parsed)
    entry = compose_question_entry(next_num, question)
    store.write_file(OPEN_QUESTIONS_MD, insert_question_entry(content, entry))

    return FlagQuestionResult(status="ok", num=next_num)


def _short_circuit_if_resolved(
    parsed: OpenQuestionsFile,
    targets: list[str],
) -> FlagQuestionResult | None:
    """Return a rejection result if any ``targets`` id is already resolved. Reads the
    working copy only, so it is best-effort: a stale local copy missing a fresh
    remote resolution falls through to the normal append path.
    """
    ambiguous = parsed.ambiguous_ids
    requested_ambiguous = [target for target in targets if target in ambiguous]
    if requested_ambiguous:
        return _reject(
            "targets contains ambiguous id(s) matching more than one entry: "
            + ", ".join(repr(target) for target in dict.fromkeys(requested_ambiguous))
            + ". The flag was not appended."
        )

    entries_by_id: dict[str, EntryBlock] = {}
    for block in parsed.blocks:
        if isinstance(block, EntryBlock):
            entries_by_id.setdefault(block.entry.id, block)

    # Iterate ``targets`` in caller order; the first resolved hit wins the
    # rejection envelope. Priority belongs to the caller, not file position.
    for target in targets:
        block = entries_by_id.get(target)
        if block is None or block.entry.resolved_by is None:
            continue
        ref = block.entry.resolved_by
        reason = (
            f"{target} is already resolved by D{ref.decision_num} on "
            f"{ref.date.isoformat()}. The flag was not appended. "
            "Working-copy freshness is bounded by the most recent pull; "
            "if a newer flag is intended despite the existing resolution, "
            "resend without targets."
        )
        return FlagQuestionResult(
            status="rejected",
            error=ErrorPayload(kind="rejected", reason=reason),
        )
    return None


def _reject(reason: str) -> FlagQuestionResult:
    return FlagQuestionResult(
        status="rejected",
        error=ErrorPayload(kind="rejected", reason=reason),
    )


def _resolve(
    store: Store,
    targets: list[str],
    resolved_by: str,
) -> FlagQuestionResult:
    """Capture store inputs, run the pure resolver, and persist changed bytes."""
    stems = store.list_decisions()
    bodies = store.read_decisions(stems)
    documents = tuple(
        ResolutionDecisionDocument(stem=stem, content=body.encode("utf-8"))
        for stem in stems
        if (body := bodies.get(stem)) is not None
    )
    content = store.read_file(OPEN_QUESTIONS_MD) or OPEN_QUESTIONS_DEFAULT_BODY
    outcome = resolve_question_document(
        open_questions_bytes=content.encode("utf-8"),
        decision_documents=documents,
        targets=targets,
        resolved_by=resolved_by,
        resolution_date=datetime.now(timezone.utc).date(),
    )
    if outcome.updated_open_questions_bytes is not None:
        store.write_file(
            OPEN_QUESTIONS_MD,
            outcome.updated_open_questions_bytes.decode("utf-8"),
        )
    return outcome.result
