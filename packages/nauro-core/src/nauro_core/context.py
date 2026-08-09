"""Context assembly: build_l0, build_l1, build_l2 from pre-loaded data.

Accepts pre-loaded file contents (``dict[str, str]``) and parsed decision
lists (``list[Decision]``) via function injection. Callers control which
files to include, allowing surface-specific customization (e.g., only L2
loads state_history.md) without nauro-core needing to know about I/O.
"""

from datetime import datetime, timezone

from nauro_core.constants import (
    L0_DECISIONS_SUMMARY_LIMIT,
    L0_QUESTIONS_LIMIT,
    L1_DECISIONS_LIMIT,
    L1_DECISIONS_SUMMARY_LIMIT,
    L1_QUESTIONS_LIMIT,
    OPEN_QUESTIONS_MD,
    PROJECT_MD,
    STACK_AUTO_CHAR_BUDGET,
    STACK_AUTO_ITEM_CHAR_LIMIT,
    STACK_AUTO_ITEM_LIMIT,
    STACK_MD,
    STACK_NON_AUTHORITATIVE_FRAMING,
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_MD,
)
from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.parsing import (
    decisions_summary_lines,
    extract_current_state,
    extract_stack_oneliner,
    is_scaffold_project_md,
    strip_leading_h1,
)
from nauro_core.questions import EntryBlock, OpenQuestionsFile, truncate_entry_text
from nauro_core.state import assemble_state_for_context

# Open questions older than this nudge the reader to close or defer.
# Q-form entries without a minted-at timestamp silently skip the projection
# until ``flag_question`` starts stamping them.
_AGE_PROJECTION_DAYS = 30

# Appended to a projection item cut at STACK_AUTO_ITEM_CHAR_LIMIT. The budget
# bounds retained file content only; the marker is an additional
# bounded-constant annotation.
_STACK_ITEM_TRUNCATION_MARKER = '... [item truncated - full text: get_raw_file("stack.md")]'


def _is_stack_heading(line: str) -> bool:
    """A top-level heading line: ``#`` at column zero."""
    return line.startswith("#")


def _is_first_level_list_item(line: str) -> bool:
    """A first-level Markdown list item: a list marker at column zero.

    Matches the unordered markers (``-``, ``*``, ``+``) and ordered markers
    (a 1-9 digit number followed by ``.`` or ``)``), each followed by a
    space. Indented (nested) list items and continuation lines never
    match — the projection carries the inventory skeleton, not nested
    rationale prose.
    """
    if line.startswith(("- ", "* ", "+ ")):
        return True
    digits = 0
    while digits < len(line) and line[digits] in "0123456789":
        digits += 1
    if not 1 <= digits <= 9:
        return False
    return line[digits : digits + 1] in (".", ")") and line[digits + 1 : digits + 2] == " "


def _fence_delimiter(line: str) -> tuple[str, int, str] | None:
    """Split a potential code-fence line into ``(char, run, rest)``.

    A fence delimiter is a run of at least three backticks or tildes
    indented at most three spaces; ``rest`` is whatever follows the run
    (the info string on an opener; must be blank on a closer).
    """
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return None
    if not stripped or stripped[0] not in "`~":
        return None
    char = stripped[0]
    run = len(stripped) - len(stripped.lstrip(char))
    if run < 3:
        return None
    return char, run, stripped[run:]


def _lines_outside_fences(lines: list[str]) -> list[str]:
    """Drop fenced code-block lines before item detection.

    Tracks the basic CommonMark fence rule: a fence opens on a run of at
    least three backticks or tildes indented at most three spaces (a
    backtick fence's info string may not itself contain a backtick — such
    a line is ordinary text; tilde info strings may), and closes only on a
    matching-or-longer run of the same character with nothing else on the
    line; an unclosed fence runs to end of file. The delimiters and
    everything between them drop out, and each elided fence leaves one
    blank line so paragraphs never merge across it.
    """
    outside: list[str] = []
    fence: tuple[str, int] | None = None
    for line in lines:
        delimiter = _fence_delimiter(line)
        if fence is None:
            if delimiter is not None and not (delimiter[0] == "`" and "`" in delimiter[2]):
                fence = delimiter[0], delimiter[1]
                outside.append("")
            else:
                outside.append(line)
        elif (
            delimiter is not None
            and delimiter[0] == fence[0]
            and delimiter[1] >= fence[1]
            and not delimiter[2].strip()
        ):
            fence = None
    return outside


def _stack_projection_items(stack: str) -> tuple[list[str], str]:
    """Deterministic walk of stack.md into projection items, in file order.

    Fenced code blocks are excluded first — their lines are never items. A
    structured file (any top-level heading or first-level list item outside
    fences) projects exactly those lines; everything nested or loose is
    excluded. An unstructured file projects its nonblank Markdown
    paragraphs instead. Returns ``(items, joiner)`` — structured items join
    line-by-line, unstructured paragraphs keep a blank line between them.
    """
    lines = _lines_outside_fences(stack.split("\n"))
    structured = [
        line.rstrip()
        for line in lines
        if _is_stack_heading(line) or _is_first_level_list_item(line)
    ]
    if structured:
        return structured, "\n"

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line.rstrip())
        elif current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs, "\n\n"


def _render_stack_projection(stack: str) -> str:
    """Render the bounded L1 stack projection block.

    Walks the projection items in file order, truncating overlong items at
    :data:`~nauro_core.constants.STACK_AUTO_ITEM_CHAR_LIMIT` retained
    characters with an explicit marker, and stops before rendering more than
    :data:`~nauro_core.constants.STACK_AUTO_ITEM_LIMIT` items or exceeding
    :data:`~nauro_core.constants.STACK_AUTO_CHAR_BUDGET` characters across
    the joined projection body — retained item text plus the inter-item
    separators the join emits. The framing line, per-item truncation
    markers, and the omitted-count trailer are bounded-constant annotations
    outside that budget. The block opens with the non-authoritative framing
    line and, when the walk stopped early, closes with the omitted-item
    count and the ``get_raw_file("stack.md")`` route to the full file.
    """
    items, joiner = _stack_projection_items(stack)

    rendered: list[str] = []
    retained_total = 0
    omitted = 0
    for idx, item in enumerate(items):
        retained = item[:STACK_AUTO_ITEM_CHAR_LIMIT]
        separator_cost = len(joiner) if rendered else 0
        if (
            len(rendered) == STACK_AUTO_ITEM_LIMIT
            or retained_total + separator_cost + len(retained) > STACK_AUTO_CHAR_BUDGET
        ):
            omitted = len(items) - idx
            break
        if len(item) > STACK_AUTO_ITEM_CHAR_LIMIT:
            rendered.append(retained.rstrip() + " " + _STACK_ITEM_TRUNCATION_MARKER)
        else:
            rendered.append(item)
        retained_total += separator_cost + len(retained)

    if not rendered:
        # An entirely fenced (or otherwise item-free) file projects the
        # framing line alone.
        return STACK_NON_AUTHORITATIVE_FRAMING

    block = STACK_NON_AUTHORITATIVE_FRAMING + "\n" + joiner.join(rendered)
    if omitted:
        block += (
            f'\n(+{omitted} more stack items — see get_raw_file("stack.md") for the full file)'
        )
    return block


def _active_decisions(decisions: list[Decision]) -> list[Decision]:
    """Filter to active decisions only."""
    return [d for d in decisions if d.status is DecisionStatus.active]


def _render_open_questions(
    parsed: OpenQuestionsFile,
    limit: int,
    include_omission_trailer: bool,
) -> str:
    """Render the first ``limit`` genuine open ``EntryBlock``s.

    Walks the parsed block list so the entry's ``timestamp`` survives for
    the age projection. Entries
    physically under ``## Resolved`` are skipped via the divider index, as
    are entries annotated ``resolved_by`` above it.
    Discovery-pointer entries (body starts with ``BRIEF:``, ``RESUME:`` or
    ``SELECT:``) are excluded entirely and do not consume a slot in the limit.
    A ``(open NN days; consider closing or deferring)`` line is prepended
    when ``entry.timestamp`` is set and the entry is older than
    :data:`_AGE_PROJECTION_DAYS`. Q-form entries without a timestamp
    render without the projection. The limit never cuts mid-entry, but
    each entry's joined rendered text is capped at
    :data:`~nauro_core.constants.QUESTION_ENTRY_CHAR_BUDGET` characters
    via :func:`~nauro_core.questions.truncate_entry_text`; entries at or
    under the budget render byte-unchanged, over-budget entries end with
    an explicit pointer at ``get_raw_file("open-questions.md")``.

    When ``include_omission_trailer`` is set and genuine entries were
    omitted beyond the limit, one trailer line reports the omitted count
    and points at ``get_raw_file`` for the full file. Pointers never count
    toward the omitted total.
    """
    divider = parsed.resolved_divider_idx
    today = datetime.now(timezone.utc).date()

    lines: list[str] = []
    rendered = 0
    omitted = 0
    for idx, block in enumerate(parsed.blocks):
        if divider is not None and idx >= divider:
            break
        if not isinstance(block, EntryBlock):
            continue
        if block.entry.resolved_by is not None:
            continue
        if block.entry.is_discovery_pointer:
            continue
        if rendered >= limit:
            omitted += 1
            continue
        entry = block.entry
        if entry.timestamp is not None:
            age_days = (today - entry.timestamp.date()).days
            if age_days > _AGE_PROJECTION_DAYS:
                lines.append(f"(open {age_days} days; consider closing or deferring)")
        lines.append(truncate_entry_text("\n".join(entry.render())))
        rendered += 1

    if include_omission_trailer and omitted:
        lines.append(
            f'(+{omitted} more open questions — see get_raw_file("open-questions.md") '
            "for the full file, including resolved history)"
        )

    return "\n".join(lines)


def _render_l0_open_questions(content: str) -> str:
    """Render the L0 open-questions body: top 3 genuine entries, no trailer."""
    if not content.strip():
        return ""
    return _render_open_questions(
        OpenQuestionsFile.parse(content),
        L0_QUESTIONS_LIMIT,
        include_omission_trailer=False,
    )


def _resolve_state(files: dict[str, str]) -> str | None:
    """Resolve state content from files dict, preferring state_current.md.

    Falls back to state.md for pre-upgrade stores. When falling back,
    uses extract_current_state() to parse out only the current section
    (legacy format may have ## Current / ## History sections).
    """
    current = files.get(STATE_CURRENT_FILENAME)
    if current is not None and current.strip():
        return current

    legacy = files.get(STATE_MD, "")
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

    Section order: project scope preamble → state → stack summary →
    open questions (top 3) → recent decisions summary (last 10 active).

    project.md leads the payload as a stable-scope preamble, with its leading
    H1 stripped (the surrounding surface supplies the title heading). An
    unedited ``nauro init`` scaffold is skipped entirely — placeholder
    prompts are not project scope.

    Args:
        files: Dict of store-relative keys to file contents.
            Recognized keys: "project.md", "state_current.md" (preferred;
            legacy "state.md"), "stack.md", "open-questions.md".
        decisions: List of parsed decision dicts (from parse_decision).
    """
    sections: list[str] = []

    project = files.get(PROJECT_MD, "")
    if project.strip() and not is_scaffold_project_md(project):
        body = strip_leading_h1(project)
        if body:
            sections.append(body)

    raw_state = _resolve_state(files)
    if raw_state:
        if STATE_CURRENT_FILENAME in files:
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

    stack = files.get(STACK_MD, "")
    oneliner = extract_stack_oneliner(stack)
    if oneliner:
        sections.append("**Stack:** " + oneliner)

    questions_content = files.get(OPEN_QUESTIONS_MD, "")
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
    """Build L1 payload (bounded working set).

    Canonical section order: project → state → stack → questions →
    full decisions (last N active) → earlier decisions summary.

    The stack section is a bounded projection of stack.md, not the raw
    file: the deterministic walk in :func:`_render_stack_projection` keeps
    the inventory skeleton under its own item and character budgets, opens
    with the non-authoritative framing line, and points at
    ``get_raw_file("stack.md")`` for anything omitted. The full file stays
    reachable via get_raw_file and L2.

    The questions section is a capped projection of genuine open entries,
    not the raw file: resolved history and discovery pointers drop out,
    the first ``L1_QUESTIONS_LIMIT`` genuine entries render in file order,
    and a single trailer line reports how many genuine entries were
    omitted. The full file stays reachable via get_raw_file and L2.

    Args:
        files: Dict of store-relative keys to file contents.
        decisions: List of parsed decision dicts.
    """
    sections: list[str] = []

    project = files.get(PROJECT_MD, "")
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

    stack = files.get(STACK_MD, "")
    if stack.strip():
        sections.append(_render_stack_projection(stack))

    questions_content = files.get(OPEN_QUESTIONS_MD, "")
    if questions_content.strip():
        parsed_questions = OpenQuestionsFile.parse(questions_content)
        rendered_questions = _render_open_questions(
            parsed_questions,
            L1_QUESTIONS_LIMIT,
            include_omission_trailer=True,
        )
        if rendered_questions:
            sections.append(parsed_questions.header + "\n" + rendered_questions)

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
    project.md verbatim and the complete stack.md body under the same
    non-authoritative framing line L1's bounded projection opens with,
    the appended state history that L1 omits, and every decision including
    superseded ones rather than L1's recent-active cap.

    Args:
        files: Dict of store-relative keys to file contents.
        decisions: List of parsed decision dicts.
    """
    sections: list[str] = []

    project = files.get(PROJECT_MD, "")
    if project.strip():
        sections.append(project.strip())

    raw_state = _resolve_state(files)
    history = files.get(STATE_HISTORY_FILENAME)
    if raw_state or history:
        assembled = assemble_state_for_context(
            raw_state, history_content=history, include_history=True
        )
        if assembled and assembled.strip():
            sections.append(assembled.strip())

    stack = files.get(STACK_MD, "")
    if stack.strip():
        sections.append(STACK_NON_AUTHORITATIVE_FRAMING + "\n" + stack.strip())

    questions_content = files.get(OPEN_QUESTIONS_MD, "")
    if questions_content.strip():
        sections.append(questions_content.strip())

    if decisions:
        parts = [d.content.strip() for d in decisions]
        sections.append("## All Decisions\n\n" + "\n\n---\n\n".join(parts))

    return "\n\n".join(sections)
