"""Context assembly: build_l0, build_l1, build_l2 from pre-loaded data.

Accepts pre-loaded file contents (dict[str, str]) and parsed decision lists
(list[dict]) via function injection. Callers control which files to include,
allowing surface-specific customization (e.g., local L0 can omit project.md
for AGENTS.md compatibility) without nauro-core needing to know about I/O.
"""

from nauro_core.constants import (
    L0_DECISIONS_SUMMARY_LIMIT,
    L0_QUESTIONS_LIMIT,
    L1_DECISIONS_LIMIT,
    L1_DECISIONS_SUMMARY_LIMIT,
)
from nauro_core.parsing import (
    decisions_summary_lines,
    extract_current_state,
    extract_stack_oneliner,
    parse_questions,
)
from nauro_core.state import assemble_state_for_context


def _active_decisions(decisions: list[dict]) -> list[dict]:
    """Filter to active decisions only."""
    return [d for d in decisions if d.get("status", "active") == "active"]


def _resolve_state(files: dict[str, str]) -> str | None:
    """Resolve state content from files dict, preferring state_current.md.

    Falls back to state.md for pre-upgrade stores. When falling back,
    uses extract_current_state() to parse out only the current section
    (legacy format may have ## Current / ## History sections).
    """
    current = files.get("state_current.md")
    if current is not None and current.strip():
        return current

    legacy = files.get("state.md", "")
    if legacy.strip():
        return legacy

    return None


def build_l0(files: dict[str, str], decisions: list[dict]) -> str:
    """Build L0 payload (concise summary).

    Section order: project → state → stack summary → open questions (top 5) →
    recent decisions summary (last 10 active).

    Args:
        files: Dict of store-relative keys to file contents.
            Recognized keys: "project.md", "state.md", "stack.md", "questions.md".
        decisions: List of parsed decision dicts (from parse_decision).
    """
    sections: list[str] = []

    project = files.get("project.md", "")
    if project.strip():
        sections.append(project.strip())

    raw_state = _resolve_state(files)
    if raw_state:
        if "state_current.md" in files:
            current = raw_state.strip()
        else:
            # Legacy fallback: parse out ## Current section
            current = extract_current_state(raw_state)
        assembled = assemble_state_for_context(
            current or None,
            history_content=None,
            include_history=False,
        )
        if assembled and assembled.strip():
            sections.append("## Current State\n" + assembled.strip())

    stack = files.get("stack.md", "")
    oneliner = extract_stack_oneliner(stack)
    if oneliner:
        sections.append("**Stack:** " + oneliner)

    questions_content = files.get("questions.md", "")
    questions = parse_questions(questions_content)
    if questions:
        top = questions[:L0_QUESTIONS_LIMIT]
        sections.append("## Open Questions\n" + "\n".join(top))

    active = _active_decisions(decisions)
    if active:
        recent = list(reversed(active))[:L0_DECISIONS_SUMMARY_LIMIT]
        lines = decisions_summary_lines(recent)
        sections.append("## Recent Decisions\n" + "\n".join(lines))

    return "\n\n".join(sections)


def build_l1(files: dict[str, str], decisions: list[dict]) -> str:
    """Build L1 payload (working set).

    Canonical section order: project → state → stack → questions →
    full decisions (last N active) → earlier decisions summary.

    Args:
        files: Dict of store-relative keys to file contents.
        decisions: List of parsed decision dicts.
    """
    sections: list[str] = []

    project = files.get("project.md", "")
    if project.strip():
        sections.append(project.strip())

    raw_state = _resolve_state(files)
    if raw_state:
        assembled = assemble_state_for_context(
            raw_state,
            history_content=None,
            include_history=False,
        )
        if assembled and assembled.strip():
            sections.append(assembled.strip())

    stack = files.get("stack.md", "")
    if stack.strip():
        sections.append(stack.strip())

    questions_content = files.get("questions.md", "")
    if questions_content.strip():
        sections.append(questions_content.strip())

    active = _active_decisions(decisions)
    if active:
        recent_full = list(reversed(active))[:L1_DECISIONS_LIMIT]
        parts = [d["content"].strip() for d in recent_full]
        sections.append("## Decisions\n\n" + "\n\n---\n\n".join(parts))

        beyond = list(reversed(active))[
            L1_DECISIONS_LIMIT : L1_DECISIONS_LIMIT + L1_DECISIONS_SUMMARY_LIMIT
        ]
        if beyond:
            lines = decisions_summary_lines(beyond)
            sections.append("## Earlier Decisions\n" + "\n".join(lines))

    return "\n\n".join(sections)


def build_l2(files: dict[str, str], decisions: list[dict]) -> str:
    """Build L2 payload (full content).

    Includes all decision content plus all files provided.

    Args:
        files: Dict of store-relative keys to file contents.
        decisions: List of parsed decision dicts.
    """
    sections: list[str] = []

    raw_state = _resolve_state(files)
    history = files.get("state_history.md")
    if raw_state or history:
        assembled = assemble_state_for_context(
            raw_state, history_content=history, include_history=True
        )
        if assembled and assembled.strip():
            sections.append(assembled.strip())

    if decisions:
        parts = [d["content"].strip() for d in decisions]
        sections.append("## All Decisions\n\n" + "\n\n---\n\n".join(parts))

    questions_content = files.get("questions.md", "")
    if questions_content.strip():
        sections.append(questions_content.strip())

    return "\n\n".join(sections)
