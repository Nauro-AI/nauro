"""Stateless markdown → structured data parsers for non-decision files.

Decision parsing lives in ``nauro_core.decision_model.parse_decision``.
This module covers the smaller helpers — filename number extraction,
state/stack/questions parsing, and snippet extraction.
"""

from __future__ import annotations

import re

from nauro_core.constants import STACK_EMPTY_MARKER


def extract_decision_number(filename: str) -> int | None:
    """Extract the decision number from a filename like ``042-some-title.md``.

    Returns None if the filename doesn't start with a number prefix.
    """
    m = re.match(r"(\d+)-", filename)
    return int(m.group(1)) if m else None


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
