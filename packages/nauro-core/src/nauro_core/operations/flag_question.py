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
from nauro_core.operations.decision_lookup import find_decision_stem_by_num
from nauro_core.operations.results import ErrorPayload, FlagQuestionResult
from nauro_core.operations.store import Store
from nauro_core.parsing import extract_decision_number
from nauro_core.question_append import (
    allocate_question_number,
    compose_question_entry,
    insert_question_entry,
)
from nauro_core.questions import EntryBlock, OpenQuestionsFile

# Private alias kept for one release: a deployed consumer still imports this
# name. New code reads OPEN_QUESTIONS_DEFAULT_BODY from nauro_core directly.
_DEFAULT_FILE_BODY = OPEN_QUESTIONS_DEFAULT_BODY

_FRESHNESS_CAVEAT = "This reads the working copy, only as fresh as the most recent pull."


def flag_question(
    store: Store,
    question: str | None = None,
    context: str | None = None,
    targets: list[str] | None = None,
    resolved_by: str | None = None,
) -> FlagQuestionResult:
    """Append *question*, or resolve the entries named in ``targets``.

    Args:
        store: Storage adapter. The kernel reads ``open-questions.md`` via
            :meth:`Store.read_file` and writes through
            :meth:`Store.write_file`. The two calls happen back-to-back
            within the same kernel invocation.
        question: Composed question body for the append action. Required
            when ``resolved_by`` is None; must be omitted when
            ``resolved_by`` is set. The adapter is responsible for any
            caller-side composition (e.g. folding ``context`` into the
            same line) before reaching the kernel.
        context: Reserved for future kernel-side composition. Currently
            unused — the adapter folds context into ``question`` and
            passes ``None`` here so the on-disk format stays unchanged.
        targets: On the append action, optional candidate ``Q###`` (or
            legacy timestamp) ids the caller suspects this question may
            duplicate. On the resolve action, the entries to stamp as
            resolved — every id must exist in the file or the whole call
            rejects.
        resolved_by: A decision identifier (``D123``, ``123``,
            ``decision-123`` …). When set, the call resolves the
            ``targets`` entries against that decision instead of
            appending. The number must resolve to a decision that exists
            in the store.

    Returns:
        :class:`FlagQuestionResult`. On the append success path
        ``status="ok"`` and ``num`` carries the minted identifier. On the
        resolve success path ``status="ok"`` and ``num`` stays unset. On
        any rejection ``status="rejected"`` and ``error`` names the
        specific failure; no write occurs.
    """
    del context  # adapter composes context into question; kernel sees one body.

    has_question = question is not None and question.strip() != ""
    if resolved_by is not None:
        if has_question:
            return _reject(
                "Pass either question (to append a new flag) or resolved_by "
                "(to resolve existing entries), not both."
            )
        return _resolve(store, targets or [], resolved_by)

    if not has_question:
        return _reject(
            "Pass either question (to append a new flag) or resolved_by "
            "(to resolve existing entries)."
        )

    assert question is not None  # narrowed by has_question above.
    content = store.read_file(OPEN_QUESTIONS_MD) or _DEFAULT_FILE_BODY
    parsed = OpenQuestionsFile.parse(content)

    if targets:
        rejection = _short_circuit_if_resolved(parsed, targets)
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
    """Stamp the ``targets`` entries as resolved by ``resolved_by`` in place. Every id
    must exist as a single entry and ``resolved_by`` must name a decision in the
    store; a bad, missing or ambiguous id rejects the whole call without writing.
    """
    num = extract_decision_number(resolved_by)
    if num is None:
        return _reject(
            f"resolved_by {resolved_by!r} is not a decision identifier "
            "(expected a form like D123, 123, or decision-123)."
        )

    if find_decision_stem_by_num(store, num) is None:
        return _reject(
            f"resolved_by names decision D{num}, which does not exist in the store. "
            f"{_FRESHNESS_CAVEAT}"
        )

    if not targets:
        return _reject("resolved_by requires at least one id in targets to resolve.")

    content = store.read_file(OPEN_QUESTIONS_MD) or _DEFAULT_FILE_BODY
    parsed = OpenQuestionsFile.parse(content)

    ambiguous = parsed.ambiguous_ids
    requested_ambiguous = [t for t in targets if t in ambiguous]
    if requested_ambiguous:
        return _reject(
            "targets contains ambiguous id(s) matching more than one entry: "
            + ", ".join(repr(t) for t in dict.fromkeys(requested_ambiguous))
            + ". Disambiguate before resolving."
        )

    entry_ids = {b.entry.id for b in parsed.blocks if isinstance(b, EntryBlock)}
    missing = [t for t in targets if t not in entry_ids]
    if missing:
        return _reject(
            "targets contains id(s) not present in open-questions.md: "
            + ", ".join(repr(t) for t in dict.fromkeys(missing))
            + f". {_FRESHNESS_CAVEAT}"
        )

    result = parsed.resolve(
        ids=targets,
        decision_num=num,
        date=datetime.now(timezone.utc).date(),
    )
    normalized = result.file.normalize()
    store.write_file(OPEN_QUESTIONS_MD, normalized.file.format())
    return FlagQuestionResult(
        status="ok",
        num=None,
        relocated_ids=normalized.relocated_ids or None,
        skipped_prose_ids=normalized.skipped_prose_ids or None,
    )
