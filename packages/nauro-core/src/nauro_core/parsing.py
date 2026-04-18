"""Stateless markdown → structured data parsers.

`parse_decision` is the primary public function: it returns a validated
``Decision`` (pydantic model) rather than a dict as it did in 0.1.x. The
name and signature are preserved for import compatibility; callers now use
attribute access (``d.status``) instead of dict access (``d["status"]``).

Other helpers in this module parse non-decision files (state, stack,
questions) and remain dict/list-based.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nauro_core.constants import STACK_EMPTY_MARKER

if TYPE_CHECKING:
    from nauro_core.decision_model import Decision


def extract_decision_number(filename: str) -> int | None:
    """Extract the decision number from a filename like ``042-some-title.md``.

    Returns None if the filename doesn't start with a number prefix.
    """
    m = re.match(r"(\d+)-", filename)
    return int(m.group(1)) if m else None


def parse_decision(content: str, filename: str) -> Decision:
    """Parse a decision markdown file into a validated ``Decision``.

    Thin wrapper around ``nauro_core.decision_model.parse_decision_v2``.
    Kept under the name ``parse_decision`` for import compatibility; callers
    moved from dict to attribute access in nauro-core 0.2.0.

    Raises:
        ValueError: malformed frontmatter / missing required sections.
        pydantic.ValidationError: field-level validation failure.
    """
    # Late import to avoid the circular
    # parsing → decision_model → parsing (extract_decision_number).
    from nauro_core.decision_model import parse_decision_v2

    return parse_decision_v2(content, filename)


def extract_current_state(state_content: str) -> str:
    """Extract only the ``## Current`` section from state.md content.

    Legacy fallback only — used when reading pre-upgrade state.md files
    that contain both ``## Current`` and ``## History`` sections. New-format
    stores use state_current.md directly.
    """
    lines = state_content.split("\n")
    in_current = False
    current_lines: list[str] = []
    for line in lines:
        if line.strip().lower() == "## current":
            in_current = True
            continue
        if in_current and line.startswith("## "):
            break
        if in_current:
            current_lines.append(line)
    return "\n".join(current_lines).strip()


def extract_stack_oneliner(stack_content: str) -> str:
    """Extract a one-line stack summary listing only technology names."""
    if not stack_content.strip() or stack_content.strip() == STACK_EMPTY_MARKER:
        return ""

    names: list[str] = []
    for line in stack_content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- ") and not line.startswith("  "):
            m = re.match(r"-\s+\*\*(.+?)\*\*", stripped)
            if m:
                names.append(m.group(1))
    return ", ".join(names)


def extract_stack_summary(stack_content: str) -> str:
    """Extract one-line tech choices from stack.md (no full reasoning)."""
    if not stack_content.strip() or stack_content.strip() == STACK_EMPTY_MARKER:
        return ""

    lines = []
    for line in stack_content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") or stripped.startswith("<!--"):
            continue
        if stripped.startswith("- ") and not line.startswith("  ") or stripped.startswith("## "):
            lines.append(stripped)
    return "\n".join(lines)


def parse_questions(content: str) -> list[str]:
    """Extract question lines from open-questions.md."""
    questions: list[str] = []
    in_resolved = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            in_resolved = "resolved" in stripped.lower()
            continue
        if in_resolved:
            continue
        if line.startswith("- ["):
            questions.append(line)
        elif line.startswith("### "):
            questions.append("- " + line.lstrip("# "))
        elif line.startswith("- ") and not line.startswith("  "):
            questions.append(line)
    return questions


def decisions_summary_lines(decisions: list, limit: int = 10) -> list[str]:
    """Build compact summary lines for decisions: ``D{num} — Title (date)``."""
    lines = []
    for d in decisions[:limit]:
        date_part = f" ({d.date})" if d.date else ""
        lines.append(f"- D{d.num} \u2014 {d.title}{date_part}")
    return lines


def extract_relevance_snippet(text: str, query_words: list[str], length: int = 100) -> str:
    """Extract ~length chars of context around the first query word match in text.

    Returns empty string if no match is found.
    """
    text_lower = text.lower()
    for word in query_words:
        pos = text_lower.find(word.lower())
        if pos != -1:
            half = length // 2
            start = max(0, pos - half)
            end = min(len(text), pos + len(word) + half)
            snippet = text[start:end].strip()
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(text) else ""
            return f"{prefix}{snippet}{suffix}"
    return ""
