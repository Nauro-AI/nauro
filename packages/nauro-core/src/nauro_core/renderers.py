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
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, field_validator

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


class _EnvelopePrologue(BaseModel):
    """The envelope fields every read-tool renderer inspects before its body.
    A non-string status, reason code or guidance is ignored rather than raised on: a raising
    renderer would trigger the callers' JSON fallback and bypass the bounded-read guards.
    """

    model_config = ConfigDict(extra="ignore")

    status: str | None = None
    reason_code: str | None = None
    guidance: str | None = None
    error: object = None
    available_files: list[str] = []

    @field_validator("status", "reason_code", "guidance", mode="before")
    @classmethod
    def text_or_none(cls, value: object) -> str | None:
        return value if isinstance(value, str) else None

    @field_validator("available_files", mode="before")
    @classmethod
    def string_entries(cls, value: object) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [entry for entry in value if isinstance(entry, str)]

    @property
    def guidance_text(self) -> str:
        return (self.guidance or "").strip()

    @property
    def disconnected_reason_code(self) -> str | None:
        """The single definition of the disconnected-envelope discriminator."""
        return self.reason_code if self.status == "error" and self.reason_code else None

    @property
    def has_error(self) -> bool:
        return "error" in self.model_fields_set


def _prologue(result: dict) -> _EnvelopePrologue:
    return _EnvelopePrologue.model_validate(result)


def disconnected_reason_code(result: dict) -> str | None:
    """Return the reason code when ``result`` is a disconnected-error envelope:
    ``status == "error"`` with a nonempty string ``reason_code``; else None.
    """
    return _prologue(result).disconnected_reason_code


def _check_decision_body(result: dict, prologue: _EnvelopePrologue) -> str:
    """Render a related-decision result for chat-UI consumption. Empty-store and
    zero-hit assessments pass through unchanged; structure comes from
    ``related_decisions``, so the markers survive an assessment-string edit.
    """
    related = result.get("related_decisions") or []
    assessment = result.get("assessment", "")

    if not related:
        # No-project guidance first, then the kernel's NO_DECISIONS_TO_CHECK
        # (empty store) / NO_RELATED_DECISIONS (no keyword match) assessment.
        # The literal fallback only fires if the envelope carries neither.
        return prologue.guidance_text or assessment.strip() or NO_RELATED_DECISIONS

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


def _get_decision_body(result: dict, prologue: _EnvelopePrologue, mode: str = "full") -> str:
    """Render a decision body.

    ``mode="full"`` gets a one-line title header; ``mode="header"`` is emitted as-is.
    """
    content = result.get("content", "") or ""
    if not content:
        guidance = prologue.guidance_text
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


def _search_decisions_body(
    result: dict, prologue: _EnvelopePrologue, query: str | None = None
) -> str:
    """Render BM25 search results. The ``query`` kwarg is display context for the
    header and takes precedence; the envelope's ``query`` key is the fallback for
    transports that carry the echo on the wire.
    """
    query = query if query is not None else result.get("query", "")
    hits = result.get("results") or []
    total = result.get("total_matches", len(hits))
    truncated = bool(result.get("truncated"))

    if not hits:
        return prologue.guidance_text or f'No matches for "{query}".'

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


def _list_decisions_body(result: dict, prologue: _EnvelopePrologue) -> str:
    """Render the project's decision list."""
    decisions = result.get("decisions") or []
    total = result.get("total", len(decisions))
    truncated = bool(result.get("truncated"))

    if not decisions:
        return prologue.guidance_text or "No decisions recorded yet."

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


def _get_context_body(
    result: dict, prologue: _EnvelopePrologue, level: str | int | None = None
) -> str:
    """Render context from either envelope key (remote ``context``, local ``content``);
    an in-budget body passes through rstripped. Over :data:`L2_CHAR_BUDGET` chars the
    size-keyed guard report renders instead on every transport; ``level`` only words it.
    """
    body = result.get("context") or result.get("content") or ""
    if not isinstance(body, str):
        body = str(body)
    if len(body) > L2_CHAR_BUDGET:
        return _context_guard_report(len(body), level)
    return body.rstrip() or prologue.guidance_text


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


def _get_raw_file_body(result: dict, prologue: _EnvelopePrologue, path: str | None = None) -> str:
    """Render a raw-file read. A hit renders via the bounded-read rules; ``path`` is a
    renderer kwarg, never an envelope field. A miss renders the error line, then the
    ``available_files`` hint in the adapter's exact order - a cross-surface contract.
    """
    content = result.get("content")
    if content is None:
        return prologue.guidance_text
    if not isinstance(content, str):
        content = str(content)
    if not isinstance(path, str):
        path = None
    return render_bounded_file(content, path)


def _diff_since_last_session_body(result: dict, prologue: _EnvelopePrologue) -> str:
    """Render a session diff.

    The ``diff`` body passes through verbatim; its sentinels are cross-surface contracts.
    """
    diff = result.get("diff") or ""
    return diff or prologue.guidance_text


def _list_projects_body(result: dict, prologue: _EnvelopePrologue) -> str:
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


_BODIES: dict[str, Callable[..., str]] = {
    "check_decision": _check_decision_body,
    "get_decision": _get_decision_body,
    "search_decisions": _search_decisions_body,
    "list_decisions": _list_decisions_body,
    "get_context": _get_context_body,
    "get_raw_file": _get_raw_file_body,
    "diff_since_last_session": _diff_since_last_session_body,
    "list_projects": _list_projects_body,
}


def _error_line(prologue: _EnvelopePrologue) -> str:
    return _error_block(prologue.error)


def _error_with_available_files(prologue: _EnvelopePrologue) -> str:
    """The error line, then the ``available_files`` hint in the adapter's exact order."""
    lines = [_error_block(prologue.error)]
    if prologue.available_files:
        lines.append("")
        lines.append("Available files:")
        lines.extend(f"  - {path}" for path in prologue.available_files)
    return "\n".join(lines)


# Tools whose error envelope carries more than the error line.
_ERROR_RENDERERS: dict[str, Callable[[_EnvelopePrologue], str]] = {
    "get_raw_file": _error_with_available_files,
}


def render(tool: str, result: dict, **options: object) -> str:
    """Render one read-tool envelope: disconnected guidance, then an error, then the body."""
    try:
        body = _BODIES[tool]
    except KeyError:
        raise ValueError(f"no renderer for tool {tool!r}") from None
    prologue = _prologue(result)
    if prologue.disconnected_reason_code is not None and prologue.guidance_text:
        return prologue.guidance_text
    if prologue.has_error:
        return _ERROR_RENDERERS.get(tool, _error_line)(prologue)
    return body(result, prologue, **options)


def render_check_decision(result: dict) -> str:
    """Render a related-decision result for chat-UI consumption."""
    return render("check_decision", result)


def render_get_decision(result: dict, mode: str = "full") -> str:
    """Render a decision body; ``mode="header"`` is emitted as-is."""
    return render("get_decision", result, mode=mode)


def render_search_decisions(result: dict, query: str | None = None) -> str:
    """Render BM25 search results; ``query`` is display context for the header."""
    return render("search_decisions", result, query=query)


def render_list_decisions(result: dict) -> str:
    """Render the project's decision list."""
    return render("list_decisions", result)


def render_get_context(result: dict, level: str | int | None = None) -> str:
    """Render context from either envelope key; ``level`` only words a guard report."""
    return render("get_context", result, level=level)


def render_get_raw_file(result: dict, path: str | None = None) -> str:
    """Render a raw-file read; ``path`` is a renderer kwarg, never an envelope field."""
    return render("get_raw_file", result, path=path)


def render_diff_since_last_session(result: dict) -> str:
    """Render a session diff; the body passes through verbatim."""
    return render("diff_since_last_session", result)


def render_list_projects(result: dict) -> str:
    """Render the user's project list as a short tabular block."""
    return render("list_projects", result)


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
    "render",
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
