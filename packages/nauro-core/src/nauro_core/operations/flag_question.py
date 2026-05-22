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
"""

from __future__ import annotations

from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.operations.results import FlagQuestionResult
from nauro_core.operations.store import Store
from nauro_core.questions import EntryBlock, OpenQuestionsFile

_DEFAULT_FILE_BODY = "# Open Questions\n"


def flag_question(
    store: Store,
    question: str,
    context: str | None = None,
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

    Returns:
        :class:`FlagQuestionResult` with ``status="ok"`` and ``num`` set
        to the newly-minted entry's sequential identifier.
    """
    del context  # adapter composes context into question; kernel sees one body.

    content = store.read_file(OPEN_QUESTIONS_MD) or _DEFAULT_FILE_BODY
    existing_nums = [
        b.entry.num
        for b in OpenQuestionsFile.parse(content).blocks
        if isinstance(b, EntryBlock) and b.entry.num is not None
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
