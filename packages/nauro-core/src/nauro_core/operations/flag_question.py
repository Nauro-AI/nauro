"""``flag_question`` — append an open question with a sequential ``Q###`` id.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`FlagQuestionResult`. The kernel owns the parse/scan/mint/insert
plumbing through the :class:`~nauro_core.operations.store.Store` protocol;
length validation, envelope-token rejection, similarity hinting, snapshot
capture, and cloud-sync push stay on the adapter side.

The insert routine mirrors the pre-cutover writer so the on-disk format
stays byte-identical: the new entry is placed directly after the file's
top-level ``# `` header, skipping blank lines and leading HTML comments.

When the caller passes ``targets``, the kernel short-circuits the append
if any named id already carries a ``resolved_by`` reference, returning a
rejection envelope naming the resolving decision. Freshness is bounded by
the working copy the Store sees — on the local stdio surface that is the
last ``_pull_on_startup`` run; on cloud HTTP MCP it is the per-request
S3 read.
"""

from __future__ import annotations

from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.operations.results import ErrorPayload, FlagQuestionResult
from nauro_core.operations.store import Store
from nauro_core.questions import EntryBlock, OpenQuestionsFile

_DEFAULT_FILE_BODY = "# Open Questions\n"


def flag_question(
    store: Store,
    question: str,
    context: str | None = None,
    targets: list[str] | None = None,
) -> FlagQuestionResult:
    """Append *question* to ``open-questions.md`` with a fresh ``Q###`` id.

    Args:
        store: Storage adapter. The kernel reads ``open-questions.md`` via
            :meth:`Store.read_file` and writes through
            :meth:`Store.write_file`. The two calls happen back-to-back
            within the same kernel invocation.
        question: Composed question body. The adapter is responsible for
            any caller-side composition (e.g. folding ``context`` into the
            same line) before reaching the kernel.
        context: Reserved for future kernel-side composition. Currently
            unused — the adapter folds context into ``question`` and
            passes ``None`` here so the on-disk format stays unchanged.
        targets: Optional list of candidate ``Q###`` (or legacy timestamp)
            ids the caller suspects this question may duplicate. When any
            named id is found in the file with ``resolved_by`` set, the
            kernel short-circuits and returns a rejection envelope naming
            the resolving decision. ``None`` and ``[]`` skip the check and
            always append. Unknown ids are ignored — the only signal is
            "this id exists and points at a decision."

    Returns:
        :class:`FlagQuestionResult`. On the success path ``status="ok"``
        and ``num`` carries the minted identifier. On the short-circuit
        path ``status="rejected"`` and ``error`` names the resolving
        decision; no write occurs.
    """
    del context  # adapter composes context into question; kernel sees one body.

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
