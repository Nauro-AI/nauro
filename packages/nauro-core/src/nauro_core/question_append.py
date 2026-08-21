"""Shared allocation, composition, and insertion for open-questions entries.

The single source for how a new ``- [Q###]`` entry enters
``open-questions.md``: ``flag_question`` appends through these helpers, and
the hosted shared-context workflow composes its discovery-pointer entry with
the same functions, so the allocated number, the entry line format, and the
insertion point cannot drift between the two writers. All functions are
pure — the caller reads and writes the file bytes.
"""

from __future__ import annotations

from nauro_core.questions import OpenQuestionsFile, format_question_id


def allocate_question_number(parsed: OpenQuestionsFile) -> int:
    """Return the next sequential ``Q`` number for *parsed*: one past the highest number
    anywhere in the file, resolved history included, so a released number is never
    reused. A number-free file allocates ``1``.
    """
    return max((entry.num for entry in parsed.numbered_entries), default=0) + 1


def compose_question_entry(num: int, body: str) -> str:
    """Compose the canonical single-line entry for *body* under ``Q{num}``."""
    return f"- [{format_question_id(num)}] {body}"


def insert_question_entry(content: str, entry: str) -> str:
    """Return *content* with *entry* inserted after the first ``# `` header,
    skipping blank lines and leading HTML comments below it; a header-less
    file applies the same skip from line two.
    """
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
    return "\n".join(lines)
