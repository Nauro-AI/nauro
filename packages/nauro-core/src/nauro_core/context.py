"""Context assembly: build_l0, build_l1, build_l2 from pre-loaded data.

Accepts pre-loaded file contents (``dict[str, str]``) and parsed decision
lists (``list[Decision]``) via function injection. Callers control which
files to include, allowing surface-specific customization (e.g., local L0
can omit project.md for AGENTS.md compatibility) without nauro-core needing
to know about I/O.
"""

from datetime import datetime, timezone

from nauro_core.constants import (
    L0_DECISIONS_SUMMARY_LIMIT,
    L0_QUESTIONS_LIMIT,
    L1_DECISIONS_LIMIT,
    L1_DECISIONS_SUMMARY_LIMIT,
    POINTER_FLAG_PREFIXES,
)
from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.parsing import (
    decisions_summary_lines,
    extract_current_state,
    extract_stack_oneliner,
)
from nauro_core.questions import EntryBlock, OpenQuestionsFile
from nauro_core.state import assemble_state_for_context

# Open questions older than this nudge the reader to close or defer.
# Q-form entries without a minted-at timestamp silently skip the projection
# until ``flag_question`` starts stamping them.
_L0_AGE_PROJECTION_DAYS = 30


def _active_decisions(decisions: list[Decision]) -> list[Decision]:
    """Filter to active decisions only."""
    return [d for d in decisions if d.status is DecisionStatus.active]


def _is_discovery_pointer(body: str) -> bool:
    """Return True when an entry body starts with a discovery-pointer prefix.

    Discovery pointers (BRIEF:/RESUME: entries) are breadcrumbs written by
    the nauro-context skill, not questions for human review, and are excluded
    from the L0 Open Questions projection.
    """
    stripped = body.lstrip()
    return stripped.startswith(POINTER_FLAG_PREFIXES)


def _render_l0_open_questions(content: str) -> str:
    """Render the first ``L0_QUESTIONS_LIMIT`` open ``EntryBlock``s for L0.

    Walks the parsed block list so the entry's ``timestamp`` survives for
    the age projection. Entries
    physically under ``## Resolved`` are skipped via the divider index.
    Discovery-pointer entries (body starts with ``BRIEF:`` or ``RESUME:``)
    are excluded entirely and do not consume a slot in the limit.
    A ``(open NN days; consider closing or deferring)`` line is prepended
    when ``entry.timestamp`` is set and the entry is older than
    :data:`_L0_AGE_PROJECTION_DAYS`. Q-form entries without a timestamp
    render without the projection.
    """
    if not content.strip():
        return ""

    parsed = OpenQuestionsFile.parse(content)
    divider = parsed.resolved_divider_idx
    today = datetime.now(timezone.utc).date()

    lines: list[str] = []
    rendered = 0
    for idx, block in enumerate(parsed.blocks):
        if divider is not None and idx >= divider:
            break
        if not isinstance(block, EntryBlock):
            continue
        if block.entry.resolved_by is not None:
            continue
        if _is_discovery_pointer(block.entry.body):
            continue
        if rendered >= L0_QUESTIONS_LIMIT:
            break
        entry = block.entry
        if entry.timestamp is not None:
            age_days = (today - entry.timestamp.date()).days
            if age_days > _L0_AGE_PROJECTION_DAYS:
                lines.append(f"(open {age_days} days; consider closing or deferring)")
        lines.extend(entry.render())
        rendered += 1

    return "\n".join(lines)


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


def _strip_leading_current_header(assembled: str) -> str:
    """Drop a leading ``# Current State`` header line from assembled state.

    state_current.md carries its own ``# Current State`` header. L0 wraps the
    state under its own ``## Current State`` section header, so without this the
    payload (and the generated AGENTS.md) shows a stuttered ``## Current State``
    immediately followed by ``# Current State``. Only the header line is removed;
    the body and any footer are preserved.
    """
    lines = assembled.strip().split("\n")
    if lines and lines[0].strip() == "# Current State":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def build_l0(files: dict[str, str], decisions: list[Decision]) -> str:
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
            body = _strip_leading_current_header(assembled)
            if body:
                sections.append("## Current State\n" + body)

    stack = files.get("stack.md", "")
    oneliner = extract_stack_oneliner(stack)
    if oneliner:
        sections.append("**Stack:** " + oneliner)

    questions_content = files.get("questions.md", "")
    rendered_questions = _render_l0_open_questions(questions_content)
    if rendered_questions:
        sections.append("## Open Questions\n" + rendered_questions)

    active = _active_decisions(decisions)
    if active:
        recent = list(reversed(active))[:L0_DECISIONS_SUMMARY_LIMIT]
        lines = decisions_summary_lines(recent)
        sections.append("## Recent Decisions\n" + "\n".join(lines))

    return "\n\n".join(sections)


def build_l1(files: dict[str, str], decisions: list[Decision]) -> str:
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
        parts = [d.content.strip() for d in recent_full]
        sections.append("## Decisions\n\n" + "\n\n---\n\n".join(parts))

        beyond = list(reversed(active))[
            L1_DECISIONS_LIMIT : L1_DECISIONS_LIMIT + L1_DECISIONS_SUMMARY_LIMIT
        ]
        if beyond:
            lines = decisions_summary_lines(beyond)
            sections.append("## Earlier Decisions\n" + "\n".join(lines))

    return "\n\n".join(sections)


def build_l2(files: dict[str, str], decisions: list[Decision]) -> str:
    """Build L2 payload (the full dump).

    Canonical section order mirrors L1: project → state (with history) →
    stack → questions → all decisions. L2 is a superset of L1: it carries
    project.md and stack.md verbatim (the loader fetches both for level 2),
    the appended state history that L1 omits, and every decision including
    superseded ones rather than L1's recent-active cap. Omitting project and
    stack here previously made the "full dump" both incomplete and smaller
    than L1.

    Args:
        files: Dict of store-relative keys to file contents.
        decisions: List of parsed decision dicts.
    """
    sections: list[str] = []

    project = files.get("project.md", "")
    if project.strip():
        sections.append(project.strip())

    raw_state = _resolve_state(files)
    history = files.get("state_history.md")
    if raw_state or history:
        assembled = assemble_state_for_context(
            raw_state, history_content=history, include_history=True
        )
        if assembled and assembled.strip():
            sections.append(assembled.strip())

    stack = files.get("stack.md", "")
    if stack.strip():
        sections.append(stack.strip())

    questions_content = files.get("questions.md", "")
    if questions_content.strip():
        sections.append(questions_content.strip())

    if decisions:
        parts = [d.content.strip() for d in decisions]
        sections.append("## All Decisions\n\n" + "\n\n---\n\n".join(parts))

    return "\n\n".join(sections)
