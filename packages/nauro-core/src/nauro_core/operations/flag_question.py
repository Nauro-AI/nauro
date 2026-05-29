"""``flag_question`` — append an open question, or resolve existing ones.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`FlagQuestionResult`. The kernel owns the parse/scan/mint/insert
plumbing through the :class:`~nauro_core.operations.store.Store` protocol;
length validation, envelope-token rejection, similarity hinting, snapshot
capture, and cloud-sync push stay on the adapter side.

Two actions share the entry point, discriminated by ``resolved_by``:

* **append** (``resolved_by`` is None) — mint a fresh ``Q###`` and insert
  it directly after the file's top-level ``# `` header, skipping blank
  lines and leading HTML comments so the on-disk format stays
  byte-identical to the pre-cutover writer. When the caller passes
  ``targets``, the append short-circuits if any named id already carries
  a ``resolved_by`` reference, returning a rejection envelope naming the
  resolving decision.

* **resolve** (``resolved_by`` is set) — stamp the entries named in
  ``targets`` as resolved by that decision, flipping each entry's
  ``resolved_by`` in place. Blocks are never relocated. Re-resolving an
  already-resolved id is idempotent (returns ok); the append-path
  already-resolved short-circuit does not apply here.

Freshness is bounded by the working copy the Store sees — on the local
stdio surface that is the last ``_pull_on_startup`` run; on cloud HTTP
MCP it is the per-request S3 read.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.operations.decision_lookup import find_decision_stem_by_num
from nauro_core.operations.results import ErrorPayload, FlagQuestionResult
from nauro_core.operations.store import Store
from nauro_core.parsing import extract_decision_number
from nauro_core.questions import EntryBlock, OpenQuestionsFile

_DEFAULT_FILE_BODY = "# Open Questions\n"

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

    existing_nums = [
        b.entry.num for b in parsed.blocks if isinstance(b, EntryBlock) and b.entry.num is not None
    ]
    next_num = max(existing_nums, default=0) + 1
    entry = f"- [Q{next_num}] {question}"

    lines = content.split("\n")
    insert_idx = 1
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_idx = i + 1
            break

    while insert_idx < len(lines) and (
        lines[insert_idx].strip() == "" or lines[insert_idx].startswith("<!--")
    ):
        insert_idx += 1

    lines.insert(insert_idx, entry)
    store.write_file(OPEN_QUESTIONS_MD, "\n".join(lines))

    return FlagQuestionResult(status="ok", num=next_num)


def _short_circuit_if_resolved(
    parsed: OpenQuestionsFile,
    targets: list[str],
) -> FlagQuestionResult | None:
    """Return a rejection result if any ``targets`` id is already resolved.

    Reads the working copy only — freshness is bounded by whatever pull
    cadence the Store's transport runs (none, on cloud HTTP; ``_pull_on_startup``
    on local stdio). The check is therefore best-effort by construction:
    a stale local copy that lacks a fresh remote resolution will fall
    through to the normal append path.
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
    """Stamp the ``targets`` entries as resolved by ``resolved_by`` in place.

    Every named id must exist in ``open-questions.md`` as a single entry,
    and ``resolved_by`` must resolve to a decision that exists in the
    store. Any unparseable identifier, missing decision, unknown target,
    or ambiguous target rejects the whole call without writing.
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
    store.write_file(OPEN_QUESTIONS_MD, result.file.format())
    return FlagQuestionResult(status="ok", num=None)
