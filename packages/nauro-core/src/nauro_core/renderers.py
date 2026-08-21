"""Human-readable renderers for MCP read-tool responses.

Each renderer is a pure function: it takes the result dict the ``tools_read``
adapter produced and returns a formatted text block for chat-UI consumption,
which the dispatcher emits as the sole ``content[0]`` block of the
``tools/call`` response. Renderers do no I/O and never mutate the input.

``check_decision`` renders a lead line (count, call-to-action, lexical-rank
caveat) then a header-equivalent triage block per hit. ``get_decision``,
``search_decisions``, ``list_decisions`` and ``list_projects`` add light
headers or short tabular listings. ``get_context`` and
``diff_since_last_session`` pass through, the latter byte-transparent to its
canonical sentinel strings. ``get_raw_file`` emits the content verbatim, or the
not-found line plus the ``available_files`` hint.
"""

from __future__ import annotations

import textwrap

from nauro_core.bounded_read import render_bounded_file
from nauro_core.constants import (
    CHARS_PER_TOKEN,
    CONTEXT_GUARD_REPORT,
    L2_CHAR_BUDGET,
    LEXICAL_RANK_CAVEAT,
    NO_RELATED_DECISIONS,
)
from nauro_core.parsing import _decision_label, is_ascii_decimal

# Width target for the rendered text blocks. Picked to fit standard
# terminal widths and Markdown chat clients without horizontal scroll.
_WIDTH = 80
_TITLE_BUDGET = 70  # Truncate titles longer than this in tabular lines.


def _id_to_label(decision_id: str) -> str:
    """Convert ``decision-145`` → ``D145``. Falls back to the raw id."""
    prefix = "decision-"
    if decision_id.startswith(prefix):
        suffix = decision_id[len(prefix) :]
        if is_ascii_decimal(suffix):
            return _decision_label(int(suffix))
    return decision_id


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an ellipsis marker."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _error_block(error) -> str:
    """Render an error field as a one-line ``Error: <reason>`` header.
    Tolerates both a plain string and the kernel's ``ErrorPayload`` dict shape.
    """
    if isinstance(error, dict):
        reason = error.get("reason") or error.get("message") or str(error)
    else:
        reason = str(error)
    return f"Error: {reason}"


def _guidance(result: dict) -> str:
    """Return the envelope's stripped ``guidance`` string, or ``""`` when absent.
    A non-string value is ignored rather than raised on: a raising renderer would
    trigger the callers' JSON fallback and bypass the bounded-read guards.
    """
    guidance = result.get("guidance")
    if not isinstance(guidance, str):
        return ""
    return guidance.strip()


def disconnected_reason_code(result: dict) -> str | None:
    """Return the reason code when ``result`` is a disconnected-error envelope:
    ``status == "error"`` with a nonempty string ``reason_code``; else None.
    This is the single definition of the envelope's discriminator.
    """
    if result.get("status") != "error":
        return None
    reason_code = result.get("reason_code")
    if isinstance(reason_code, str) and reason_code:
        return reason_code
    return None


def _connection_guidance(result: dict) -> str:
    if disconnected_reason_code(result) is not None:
        return _guidance(result)
    return ""


def render_check_decision(result: dict) -> str:
    """Render a related-decision result for chat-UI consumption. Empty-store and
    zero-hit assessments pass through unchanged; structure comes from
    ``related_decisions``, so the markers survive an assessment-string edit.
    """
    if guidance := _connection_guidance(result):
        return guidance
    if "error" in result:
        return _error_block(result["error"])

    related = result.get("related_decisions") or []
    assessment = result.get("assessment", "")

    if not related:
        # No-project guidance first, then the kernel's NO_DECISIONS_TO_CHECK
        # (empty store) / NO_RELATED_DECISIONS (no keyword match) assessment.
        # The literal fallback only fires if the envelope carries neither.
        return _guidance(result) or assessment.strip() or NO_RELATED_DECISIONS

    lines: list[str] = []
    count = len(related)
    # One honest lead line: the count, the actionable call-to-action, and the
    # lexical-rank caveat so the ranking is not read as a relevance verdict.
    # Prefer the upstream assessment's "Call ..." sentence so the get_decision
    # number and pluralization stay in sync with the kernel; fall back to a
    # generic prompt.
    cta = (
        _extract_call_to_action(assessment)
        or "Call get_decision (mode=full) on each decision you reason about before proposing."
    )
    lead = (
        f"{count} related {'decision' if count == 1 else 'decisions'}. {cta} {LEXICAL_RANK_CAVEAT}"
    )
    lines.append(lead)
    lines.append("")

    for idx, hit in enumerate(related):
        lines.extend(_check_hit_block(hit, is_top=idx == 0))
        lines.append("")

    return "\n".join(lines).rstrip()


def _check_hit_block(hit: dict, *, is_top: bool) -> list[str]:
    """Render one hit as a header-equivalent triage block, mirroring
    ``get_decision``'s header mode so it substitutes for a ``mode=header`` call:
    the title is untruncated and supersession refs appear only when present.
    """
    label = _id_to_label(hit.get("id", ""))
    status = hit.get("status", "")
    score = hit.get("score", 0.0)
    title = hit.get("title", "") or "(no title)"
    score_str = f"BM25 {score:5.2f}" if isinstance(score, (int, float)) else "BM25 ?"
    marker = "    <- top match" if is_top else ""

    lines = [f"  - {label}  [{status}]  {score_str}{marker}", f"    {title}"]

    triage_parts = [
        f"{prefix} {value}"
        for prefix, value in (
            ("decided", hit.get("date", "")),
            ("type", hit.get("decision_type", "")),
            ("confidence", hit.get("confidence", "")),
        )
        if value
    ]
    if triage_parts:
        lines.append(f"    {'  '.join(triage_parts)}")

    supersession_parts = [
        f"{prefix} {value}"
        for prefix, value in (
            ("supersedes", hit.get("supersedes", "")),
            ("superseded_by", hit.get("superseded_by", "")),
        )
        if value
    ]
    if supersession_parts:
        lines.append(f"    {'  '.join(supersession_parts)}")

    preview = (hit.get("rationale_preview") or "").strip()
    if preview:
        # Keep paths and long identifiers intact: never split inside a word
        # or at a hyphen, even when that costs an overlong line.
        wrapped = textwrap.wrap(
            preview.replace("\n", " "),
            _WIDTH - 4,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.extend(f"    {segment}" for segment in wrapped)
    return lines


def _extract_call_to_action(assessment: str) -> str:
    """Pull the trailing ``Call ...`` sentence from the assessment, or return
    ``""`` so the renderer's default call-to-action fires in the lead line.
    """
    idx = assessment.find("Call ")
    if idx == -1:
        return ""
    return assessment[idx:].strip()


def render_get_decision(result: dict, mode: str = "full") -> str:
    """Render a decision body.

    ``mode="full"`` gets a one-line title header; ``mode="header"`` is emitted as-is.
    """
    if guidance := _connection_guidance(result):
        return guidance
    if "error" in result:
        return _error_block(result["error"])

    content = result.get("content", "") or ""
    if not content:
        guidance = _guidance(result)
        if guidance:
            return guidance
    if mode == "header":
        return content.rstrip()

    header = _decision_title_header(content)
    if header:
        return f"{header}\n\n{content}".rstrip()
    return content.rstrip()


def _decision_title_header(body: str) -> str:
    """Pull the ``# NNN - Title`` line from a decision body.

    Returns ``""`` when the body is malformed or carries no decision-style heading.
    """
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("# "):
            return stripped
        # Stop scanning past the first non-frontmatter, non-blank line that
        # is not a heading; decisions always lead with the title.
        if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
            break
    return ""


def render_search_decisions(result: dict, query: str | None = None) -> str:
    """Render BM25 search results. The ``query`` kwarg is display context for the
    header and takes precedence; the envelope's ``query`` key is the fallback for
    transports that carry the echo on the wire.
    """
    if guidance := _connection_guidance(result):
        return guidance
    if "error" in result:
        return _error_block(result["error"])

    query = query if query is not None else result.get("query", "")
    hits = result.get("results") or []
    total = result.get("total_matches", len(hits))
    truncated = bool(result.get("truncated"))

    if not hits:
        return _guidance(result) or f'No matches for "{query}".'

    lines: list[str] = []
    header = f'Found {total} match{"" if total == 1 else "es"} for "{query}":'
    lines.append(header)
    lines.append("")

    for hit in hits:
        number = hit.get("number")
        label = _decision_label(number) if isinstance(number, int) else "D???"
        status = hit.get("status", "")
        score = hit.get("score", 0.0)
        score_str = f"BM25 {score:5.2f}" if isinstance(score, (int, float)) else "BM25 ?"
        title = hit.get("title", "") or "(no title)"
        snippet = (hit.get("relevance_snippet") or "").strip()

        lines.append(f"  - {label}  [{status}]  {score_str}")
        lines.append(f"    {_truncate(title, _TITLE_BUDGET)}")
        if snippet:
            snippet_line = _truncate(snippet.replace("\n", " "), _WIDTH - 6)
            lines.append(f'    "{snippet_line}"')
        lines.append("")

    if truncated:
        lines.append(f"Results truncated at {len(hits)} of {total} — raise limit to see more.")

    return "\n".join(lines).rstrip()


def render_list_decisions(result: dict) -> str:
    """Render the project's decision list."""
    if guidance := _connection_guidance(result):
        return guidance
    if "error" in result:
        return _error_block(result["error"])

    decisions = result.get("decisions") or []
    total = result.get("total", len(decisions))
    truncated = bool(result.get("truncated"))

    if not decisions:
        return _guidance(result) or "No decisions recorded yet."

    lines: list[str] = []
    lines.append(f"Decisions ({total} total):")
    lines.append("")

    for d in decisions:
        number = d.get("number")
        label = _decision_label(number) if isinstance(number, int) else "D???"
        status = d.get("status", "")
        title = d.get("title", "") or "(no title)"
        lines.append(f"  - {label}  [{status:<10}]  {_truncate(title, _TITLE_BUDGET)}")

    if truncated:
        lines.append("")
        lines.append(f"Showing {len(decisions)} of {total} decisions — raise limit to see more.")

    return "\n".join(lines).rstrip()


def render_get_context(result: dict, level: str | int | None = None) -> str:
    """Render context from either envelope key (remote ``context``, local ``content``);
    an in-budget body passes through rstripped. Over :data:`L2_CHAR_BUDGET` chars the
    size-keyed guard report renders instead on every transport; ``level`` only words it.
    """
    if guidance := _connection_guidance(result):
        return guidance
    if "error" in result:
        return _error_block(result["error"])
    body = result.get("context") or result.get("content") or ""
    if not isinstance(body, str):
        body = str(body)
    if len(body) > L2_CHAR_BUDGET:
        return _context_guard_report(len(body), level)
    return body.rstrip() or _guidance(result)


def _context_guard_report(size: int, level: str | int | None) -> str:
    """Compact size-and-recovery report replacing an over-budget context body."""
    if isinstance(level, bool):
        level_clause = ""
    elif isinstance(level, int):
        level_clause = f" (level L{level})"
    elif isinstance(level, str) and level.strip():
        level_clause = f" (level {level.strip()})"
    else:
        level_clause = ""
    return CONTEXT_GUARD_REPORT.format(
        level_clause=level_clause,
        chars=f"{size:,}",
        tokens=f"{size // CHARS_PER_TOKEN:,}",
        budget=f"{L2_CHAR_BUDGET:,}",
    )


def render_get_raw_file(result: dict, path: str | None = None) -> str:
    """Render a raw-file read. A hit renders via the bounded-read rules; ``path`` is a
    renderer kwarg, never an envelope field. A miss renders the error line, then the
    ``available_files`` hint in the adapter's exact order - a cross-surface contract.
    """
    if guidance := _connection_guidance(result):
        return guidance
    if "error" in result:
        lines = [_error_block(result["error"])]
        # Tolerate a malformed hint: a non-list value or non-string entries
        # are dropped rather than raised on — a raising renderer would fall
        # back to the full JSON envelope and defeat the bounded render.
        available = result.get("available_files")
        hint_paths = (
            [entry for entry in available if isinstance(entry, str)]
            if isinstance(available, (list, tuple))
            else []
        )
        if hint_paths:
            lines.append("")
            lines.append("Available files:")
            lines.extend(f"  - {path}" for path in hint_paths)
        return "\n".join(lines)
    content = result.get("content")
    if content is None:
        return _guidance(result)
    if not isinstance(content, str):
        content = str(content)
    if not isinstance(path, str):
        path = None
    return render_bounded_file(content, path)


def render_diff_since_last_session(result: dict) -> str:
    """Render a session diff.

    The ``diff`` body passes through verbatim; its sentinels are cross-surface contracts.
    """
    if guidance := _connection_guidance(result):
        return guidance
    if "error" in result:
        return _error_block(result["error"])
    diff = result.get("diff") or ""
    return diff or _guidance(result)


def render_list_projects(result: dict) -> str:
    """Render the user's project list as a short tabular block."""
    projects = result.get("projects") or []
    if not projects:
        return (
            "No projects yet. Run `nauro init <name>` to create one, or "
            "`nauro attach <project_id>` to connect to an existing one."
        )

    lines: list[str] = []
    lines.append(f"Projects ({len(projects)}):")
    lines.append("")
    for p in projects:
        name = p.get("name", "") or "(no name)"
        role = p.get("role", "")
        pid = p.get("project_id", "")
        lines.append(f"  - {name:<32}  {role:<7}  {pid}")
    return "\n".join(lines).rstrip()


# Renderer registry used by the dispatcher: every read tool renders a
# single human-readable text block through it.
RENDERERS = {
    "check_decision": render_check_decision,
    "get_decision": render_get_decision,
    "search_decisions": render_search_decisions,
    "list_decisions": render_list_decisions,
    "get_context": render_get_context,
    "get_raw_file": render_get_raw_file,
    "diff_since_last_session": render_diff_since_last_session,
    "list_projects": render_list_projects,
}


__all__ = [
    "RENDERERS",
    "disconnected_reason_code",
    "render_check_decision",
    "render_diff_since_last_session",
    "render_get_context",
    "render_get_decision",
    "render_get_raw_file",
    "render_list_decisions",
    "render_list_projects",
    "render_search_decisions",
]
