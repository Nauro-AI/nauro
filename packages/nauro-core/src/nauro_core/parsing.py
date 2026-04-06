"""Pure string-to-dict parsing: parse_decision, parse_questions, etc.

Stateless functions that accept markdown strings and return structured
dicts. No I/O, no filesystem access — callers are responsible for loading
the raw text before calling these parsers.
"""

import logging
import re

from nauro_core.constants import STACK_EMPTY_MARKER

logger = logging.getLogger(__name__)


def extract_decision_number(filename: str) -> int | None:
    """Extract the decision number from a filename like ``042-some-title.md``.

    Returns None if the filename doesn't start with a number prefix.
    """
    m = re.match(r"(\d+)-", filename)
    return int(m.group(1)) if m else None


def strip_frontmatter(content: str) -> str:
    """Strip YAML frontmatter from content if present."""
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            return content[end + 5 :]
    return content


def parse_metadata_field(body: str, field: str) -> str | None:
    """Extract a bold metadata field value from decision body.

    Matches patterns like: ``**Field:** value``
    Returns None if the field is not found.
    """
    m = re.search(rf"\*\*{re.escape(field)}:\*\*\s*(.*)", body)
    return m.group(1).strip() if m else None


def parse_decision(content: str, filename: str) -> dict:
    """Parse a decision markdown file into a structured dict.

    Handles both old format (YAML frontmatter + ## Rationale) and new format
    (bold metadata lines + ## Decision + ## Rejected Alternatives).

    Reconciled divergences:
    - superseded_by/supersedes: both repos now parse these (remote was missing them)
    - status default: canonicalized to ``status or "active"`` (remote used
      ``d.get("status") == "active"`` which excluded decisions with no status field)

    Returns a dict with keys: num, title, rationale, content, date, decision_type,
    reversibility, source, confidence, files_affected, version, status,
    superseded_by, supersedes, body.
    """
    num = extract_decision_number(filename) or 0

    body = strip_frontmatter(content)

    title = ""
    rationale = ""
    in_rationale = False
    in_decision = False
    for line in body.split("\n"):
        if line.startswith("# ") and not title:
            # Strict: expect em-dash (U+2014) separator per format contract
            strict = re.sub(r"^# \d+\s+\u2014\s+", "", line)
            if strict != line:
                title = strict.strip()
            else:
                # Fallback: accept colon or other separators, but log
                title = re.sub(r"^# \d+[:\s\u2014]+\s*", "", line).strip()
                if title != line.lstrip("# ").strip():
                    logger.debug("Non-standard title separator in %s", filename)
        elif line.startswith("## Rationale"):
            in_rationale = True
            in_decision = False
            continue
        elif line.startswith("## Decision"):
            in_decision = True
            in_rationale = False
            continue
        elif line.startswith("## ") and (in_rationale or in_decision):
            in_rationale = False
            in_decision = False
        elif (in_rationale or in_decision) and line.strip():
            rationale += line.strip() + " "

    date = parse_metadata_field(body, "Date")
    decision_type = parse_metadata_field(body, "Type")
    reversibility = parse_metadata_field(body, "Reversibility")
    source = parse_metadata_field(body, "Source")
    confidence = parse_metadata_field(body, "Confidence")

    files_affected_raw = parse_metadata_field(body, "Files affected")
    files_affected = (
        [f.strip() for f in files_affected_raw.split(",")] if files_affected_raw else None
    )

    version = parse_metadata_field(body, "Version")
    version_int = int(version) if version and version.isdigit() else 1
    status = parse_metadata_field(body, "Status") or "active"

    # Versioning fields — reconciled: remote was missing these
    superseded_by = parse_metadata_field(body, "Superseded by")
    supersedes = parse_metadata_field(body, "Supersedes")

    return {
        "num": num,
        "title": title,
        "rationale": rationale.strip(),
        "content": content,
        "body": body,
        "date": date,
        "decision_type": decision_type,
        "reversibility": reversibility,
        "source": source,
        "confidence": confidence,
        "files_affected": files_affected,
        "version": version_int,
        "status": status,
        "superseded_by": superseded_by,
        "supersedes": supersedes,
    }


def extract_current_state(state_content: str) -> str:
    """Extract only the ``## Current`` section from state.md content.

    Returns the text between ``## Current`` and the next ``##`` heading
    (or end of file). Returns empty string if no ``## Current`` section
    is found, allowing callers to fall back to full content.
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
    """Extract a one-line stack summary listing only technology names.

    Parses bold items (``**Name**``) from top-level bullet lines in
    stack.md and joins them with commas. Returns empty string for
    empty or placeholder stacks.
    """
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
    """Extract question lines from open-questions.md.

    Handles multiple formats:
    - Checkbox lines: ``- [2026-01-01 UTC] Question text``
    - H3 headings: ``### Question title``
    - Plain bullets: ``- Question text``

    Skips lines under a ``## Resolved`` section.
    """
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


def decisions_summary_lines(decisions: list[dict], limit: int = 10) -> list[str]:
    """Build compact summary lines for decisions: ``D{num} — Title (date)``."""
    lines = []
    for d in decisions[:limit]:
        date_part = f" ({d['date']})" if d.get("date") else ""
        lines.append(f"- D{d['num']} \u2014 {d['title']}{date_part}")
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
