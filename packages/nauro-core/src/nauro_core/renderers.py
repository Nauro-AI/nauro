"""Human-readable renderers for MCP read-tool responses.

Each renderer is a pure function: takes the result dict that the
``tools_read`` adapter (or ``list_user_projects``) produced and returns a
formatted text block intended for chat-UI consumption. The dispatcher
emits the rendered text as the sole ``content[0]`` block of the MCP
``tools/call`` response.

Renderers must not perform I/O, hit S3/DDB, or import anything that
would. They must not mutate the input dict.

Per-tool surface area:

* ``check_decision`` — top hit with rationale preview + lower hits as a
  short list; the agent-facing call-to-action footer comes from the
  upstream ``assessment`` field.
* ``get_decision`` — light header on top of the markdown body the kernel
  already returns.
* ``search_decisions`` — query echo, then a ranked short list of hits
  with snippet for each.
* ``list_decisions`` — short tabular list of ``(D###, status, title)``
  rows. Empty-state guidance flows through unchanged.
* ``get_context`` — passthrough; the kernel-assembled markdown is already
  human-readable.
* ``list_projects`` — short tabular list of ``(name, role, project_id)``
  rows so the agent can disambiguate without re-fetching.
"""

from __future__ import annotations

# Width target for the rendered text blocks. Picked to fit standard
# terminal widths and Markdown chat clients without horizontal scroll.
_WIDTH = 80
_TITLE_BUDGET = 70  # Truncate titles longer than this in tabular lines.


def _id_to_label(decision_id: str) -> str:
    """Convert ``decision-145`` → ``D145``. Falls back to the raw id."""
    prefix = "decision-"
    if decision_id.startswith(prefix):
        suffix = decision_id[len(prefix) :]
        if suffix.isdigit():
            return f"D{int(suffix):03d}"
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

    Most adapters lift ``result.error.reason`` to a top-level string, but
    ``check_decision`` carries the kernel's full :class:`ErrorPayload`
    serialization (``{"kind": "...", "reason": "..."}``). Tolerate both.
    """
    if isinstance(error, dict):
        reason = error.get("reason") or error.get("message") or str(error)
    else:
        reason = str(error)
    return f"Error: {reason}"


def render_check_decision(result: dict) -> str:
    """Render the conflict-check result for chat-UI consumption.

    Empty-store and zero-hit assessments flow through unchanged. The
    rendered list pulls structure from ``related_decisions`` so the
    top-match marker and rationale preview render even when an upstream
    assessment-string edit drifts.
    """
    if "error" in result:
        return _error_block(result["error"])

    related = result.get("related_decisions") or []
    assessment = result.get("assessment", "")

    if not related:
        # NO_DECISIONS_TO_CHECK / "No related decisions found." cases.
        return assessment.strip() or "No related decisions found."

    lines: list[str] = []
    count = len(related)
    if count == 1:
        lines.append("Found 1 related decision:")
    else:
        lines.append(f"Found {count} related decisions:")
    lines.append("")

    for idx, hit in enumerate(related):
        label = _id_to_label(hit.get("id", ""))
        status = hit.get("status", "")
        score = hit.get("score", 0.0)
        title = hit.get("title", "") or "(no title)"
        is_top = idx == 0
        score_str = f"BM25 {score:5.2f}" if isinstance(score, (int, float)) else "BM25 ?"
        marker = "    <- top match" if is_top else ""
        header = f"  - {label}  [{status}]  {score_str}{marker}"
        lines.append(header)
        lines.append(f"    {_truncate(title, _TITLE_BUDGET)}")
        if is_top:
            preview = (hit.get("rationale_preview") or "").strip()
            if preview:
                preview_line = _truncate(preview.replace("\n", " "), _WIDTH - 6)
                lines.append(f'    "{preview_line}"')
        lines.append("")

    # Call-to-action footer. Prefer the upstream assessment's "Call ..."
    # sentence so the get_decision number and pluralization stay in sync
    # with what the kernel decided. Fall back to a generic prompt.
    footer = _extract_call_to_action(assessment)
    if footer:
        lines.append(footer)
    else:
        lines.append("Call get_decision on each related decision before proposing.")

    return "\n".join(lines).rstrip()


def _extract_call_to_action(assessment: str) -> str:
    """Pull the trailing ``Call get_decision...`` sentence from the assessment.

    The kernel always emits a single ``Call ...`` clause as the last
    sentence; if the format ever drifts, fall back to an empty string and
    let the renderer default footer fire.
    """
    idx = assessment.find("Call ")
    if idx == -1:
        return ""
    return assessment[idx:].strip()


def render_get_decision(result: dict, mode: str = "full") -> str:
    """Render a decision body.

    For ``mode="full"`` the kernel returns the verbatim markdown body;
    surface it under a one-line header so chat clients see the title at a
    glance. For ``mode="header"`` the kernel already returns the compact
    projection (triage frontmatter + title + lede), so emit it as-is — a
    second title header would duplicate the projection's own title line.
    """
    if "error" in result:
        return _error_block(result["error"])

    content = result.get("content", "") or ""
    if mode == "header":
        return content.rstrip()

    header = _decision_title_header(content)
    if header:
        return f"{header}\n\n{content}".rstrip()
    return content.rstrip()


def _decision_title_header(body: str) -> str:
    """Pull the ``# NNN - Title`` line from a decision body.

    Returns an empty string when the body is malformed or missing a
    decision-style heading.
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


def render_search_decisions(result: dict) -> str:
    """Render BM25 search results."""
    if "error" in result:
        return _error_block(result["error"])

    query = result.get("query", "")
    hits = result.get("results") or []
    total = result.get("total_matches", len(hits))
    truncated = bool(result.get("truncated"))

    if not hits:
        return f'No matches for "{query}".'

    lines: list[str] = []
    header = f'Found {total} match{"" if total == 1 else "es"} for "{query}":'
    lines.append(header)
    lines.append("")

    for hit in hits:
        number = hit.get("number")
        label = f"D{number:03d}" if isinstance(number, int) else "D???"
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
    if "error" in result:
        return _error_block(result["error"])

    decisions = result.get("decisions") or []
    total = result.get("total", len(decisions))
    truncated = bool(result.get("truncated"))

    if not decisions:
        guidance = (result.get("guidance") or "").strip()
        return guidance or "No decisions recorded yet."

    lines: list[str] = []
    lines.append(f"Decisions ({total} total):")
    lines.append("")

    for d in decisions:
        number = d.get("number")
        label = f"D{number:03d}" if isinstance(number, int) else "D???"
        status = d.get("status", "")
        title = d.get("title", "") or "(no title)"
        lines.append(f"  - {label}  [{status:<10}]  {_truncate(title, _TITLE_BUDGET)}")

    if truncated:
        lines.append("")
        lines.append(f"Showing {len(decisions)} of {total} decisions — raise limit to see more.")

    return "\n".join(lines).rstrip()


def render_get_context(result: dict) -> str:
    """Render context. The kernel already assembles human-readable markdown;
    pass it through, with structured errors surfaced explicitly.

    The two adapters disagree on the envelope key: the remote MCP server
    surfaces the assembled markdown under ``context``; the local stdio
    server uses ``content`` (the kernel ``GetContextResult`` field name).
    Accept either so a single renderer covers both transports.
    """
    if "error" in result:
        return _error_block(result["error"])
    body = result.get("context") or result.get("content") or ""
    return body.rstrip()


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


# Renderer registry used by the dispatcher. Only read tools whose JSON
# envelope is unhelpful as primary content appear here; ``get_raw_file``
# and ``diff_since_last_session`` already return user-rendered bodies and
# are intentionally absent.
RENDERERS = {
    "check_decision": render_check_decision,
    "get_decision": render_get_decision,
    "search_decisions": render_search_decisions,
    "list_decisions": render_list_decisions,
    "get_context": render_get_context,
    "list_projects": render_list_projects,
}


__all__ = [
    "RENDERERS",
    "render_check_decision",
    "render_get_context",
    "render_get_decision",
    "render_list_decisions",
    "render_list_projects",
    "render_search_decisions",
]
