"""Nauro MCP server — stdio transport for Claude Code integration.

Spawned by Claude Code at session start, communicates over stdin/stdout.
Same tools as the HTTP server, same store, same payloads.

MCP tools (11 total — 7 read, 4 write):
  - get_context(project, level)          → Return project context at L0/L1/L2
  - get_raw_file(project, path)          → Return raw file from project store
  - list_decisions(project, ...)         → Browse decision history
  - get_decision(project, number)        → Get full decision by number
  - diff_since_last_session(project, ..) → Show what changed since last session
  - search_decisions(project, query)     → Search decisions by keyword
  - propose_decision(project, ...)       → Propose a decision with validation
  - confirm_decision(confirm_id)         → Confirm a pending decision
  - check_decision(proposed_approach)    → Check for conflicts without writing
  - flag_question(project, ...)          → Flag an open question for human review
  - update_state(project, ...)           → Update current project state
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server import FastMCP

from nauro.mcp.tools import (
    tool_check_decision,
    tool_confirm_decision,
    tool_diff_since_last_session,
    tool_flag_question,
    tool_get_context,
    tool_get_decision,
    tool_get_raw_file,
    tool_list_decisions,
    tool_propose_decision,
    tool_search_decisions,
    tool_update_state,
)
from nauro.onboarding import WELCOME_NO_PROJECT
from nauro.store.registry import get_store_path, resolve_project

logger = logging.getLogger("nauro.stdio")
mcp = FastMCP("nauro", log_level="WARNING")


def _resolve_store(project: str | None, cwd: str | None) -> Path:
    """Resolve project name to store path, raising on failure."""
    name = project
    if not name and cwd:
        name = resolve_project(Path(cwd))
    if not name:
        raise ValueError("Could not resolve project. Pass a 'project' name or 'cwd' path.")
    store_path = get_store_path(name)
    if not store_path.exists():
        raise ValueError(f"Project store not found: {name}")
    return store_path


@mcp.tool()
def get_context(project: str | None = None, cwd: str | None = None, level: int = 0) -> str:
    """Return project context at the requested detail level.

    L0 includes the last 10 active decisions with titles and dates.
    Do NOT call list_decisions after get_context unless you need decisions
    beyond the last 10 or need the include_superseded filter.

    Args:
        project: Project name (e.g. "nauro"). If omitted, resolved from cwd.
        cwd: Working directory path for project resolution fallback.
        level: Detail level — 0 (concise summary), 1 (working set), 2 (full dump).
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return WELCOME_NO_PROJECT
    return tool_get_context(store_path, level)


@mcp.tool()
def get_raw_file(path: str, project: str | None = None, cwd: str | None = None) -> dict:
    """Returns the raw content of any file in the Nauro project store.

    Paths include: project.md, state.md, questions.md, references.md,
    decisions/042-some-decision.md

    This is a low-level escape hatch. For most use cases, prefer:
    - get_context for project overview, state, questions, and recent decisions
    - get_decision for a specific decision by number
    - search_decisions for finding decisions by topic

    Args:
        path: File path relative to project root (e.g., 'project.md').
        project: Project name (e.g. "nauro"). If omitted, resolved from cwd.
        cwd: Working directory path for project resolution fallback.
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_get_raw_file(store_path, path)


@mcp.tool()
def list_decisions(
    project: str | None = None,
    cwd: str | None = None,
    limit: int = 20,
    include_superseded: bool = False,
) -> dict:
    """Browse the full decision history.

    Use when you need decisions beyond the last 10 included in get_context,
    or when you need the include_superseded filter.

    Args:
        project: Project name. If omitted, resolved from cwd.
        cwd: Working directory path for project resolution fallback.
        limit: Max decisions to return (default 20).
        include_superseded: Include superseded decisions (default false).
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_list_decisions(store_path, limit, include_superseded)


@mcp.tool()
def get_decision(
    number: int,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Get the full content of a specific decision by number.

    Args:
        number: Decision number (e.g., 23).
        project: Project name. If omitted, resolved from cwd.
        cwd: Working directory path for project resolution fallback.
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_get_decision(store_path, number)


@mcp.tool()
def diff_since_last_session(
    project: str | None = None,
    cwd: str | None = None,
    days: int | None = None,
) -> dict:
    """Show what changed in the project context since the last snapshot.

    When days is omitted, diffs the two most recent snapshots (session-scoped).
    When days is provided, finds the nearest snapshot to N days ago and diffs
    against the current state.

    Args:
        project: Project name. If omitted, resolved from cwd.
        cwd: Working directory path for project resolution fallback.
        days: Optional: number of days to look back.
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_diff_since_last_session(store_path, days)


@mcp.tool()
def propose_decision(
    project: str,
    title: str,
    rationale: str,
    rejected: list[dict] | None = None,
    confidence: str = "medium",
    decision_type: str | None = None,
    reversibility: str | None = None,
    files_affected: list[str] | None = None,
    skip_validation: bool = False,
) -> dict:
    """Propose a new architectural decision for validation and recording.

    Runs a validation pipeline before writing:
    - Tier 1: Structural validation (required fields)
    - Tier 2: Similarity check against existing decisions
    - Tier 3: Conflict detection against existing decisions

    Returns validation results (similar decisions, conflicts, assessment)
    and a confirm_id. The decision is NOT written until confirm_decision
    is called with this confirm_id.

    If you already called check_decision for this approach, pass
    skip_validation=true to skip redundant tier-2/tier-3 matching.
    Tier-1 structural validation always runs regardless.

    Use check_decision first for advisory "would this conflict?" checks.
    Use propose_decision when ready to write.

    Args:
        project: Project name.
        title: Short title for the decision.
        rationale: Why this decision was made, including constraints and tradeoffs.
        rejected: Alternatives considered and rejected, each with "alternative" and "reason".
        confidence: high, medium, or low.
        decision_type: architecture, library_choice, pattern, refactor,
            api_design, infrastructure, data_model.
        reversibility: easy, moderate, or hard.
        files_affected: Key file paths affected by this decision.
        skip_validation: Skip tier-2/tier-3 validation. Tier-1 structural
            checks always run. Use when you already called check_decision.
    """
    try:
        store_path = _resolve_store(project, None)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_propose_decision(
        store_path,
        title=title,
        rationale=rationale,
        rejected=rejected,
        confidence=confidence,
        decision_type=decision_type,
        reversibility=reversibility,
        files_affected=files_affected,
        skip_validation=skip_validation,
    )


@mcp.tool()
def confirm_decision(
    confirm_id: str,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Confirm a previously proposed decision after reviewing the validation results.

    Only needed when propose_decision returns status=pending_confirmation.

    Args:
        confirm_id: The confirm_id returned by propose_decision.
        project: Project name (for store resolution).
        cwd: Working directory path (fallback for project resolution).
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_confirm_decision(store_path, confirm_id)


@mcp.tool()
def check_decision(
    proposed_approach: str,
    context: str | None = None,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Check whether a proposed approach conflicts with existing decisions WITHOUT writing anything.

    Use this to consult the project's decision history before committing to an approach.

    Args:
        proposed_approach: Description of the approach you're considering.
        context: Optional additional context about why you're considering this approach.
        project: Project name.
        cwd: Working directory path (fallback for project resolution).
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_check_decision(store_path, proposed_approach, context)


@mcp.tool()
def search_decisions(
    query: str,
    limit: int = 10,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Search across all project decisions by keyword. Returns decisions whose
    titles or rationale contain the search terms (case-insensitive substring
    matching). Includes both active and superseded decisions.

    Use when you need to find decisions about a specific topic rather than
    browsing the full list. More token-efficient than list_decisions for
    targeted lookups.

    Example: search_decisions("authentication") returns all decisions
    related to auth, OAuth, JWT, etc.

    Returns: decision number, title, date, status, and a relevance snippet
    from the matching rationale.

    Requires a non-empty query. Use list_decisions to browse all decisions.

    Args:
        query: Search text (required, non-empty).
        limit: Maximum results to return (default 10).
        project: Project name. If omitted, resolved from cwd.
        cwd: Working directory path for project resolution fallback.
    """
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_search_decisions(store_path, query, limit)


@mcp.tool()
def flag_question(
    project: str,
    question: str,
    context: str | None = None,
) -> str:
    """Flag an open question for human review.

    Checks if the question is already addressed by an existing decision.

    Args:
        project: Project name.
        question: The question to flag.
        context: Optional context about why this question matters.
    """
    try:
        store_path = _resolve_store(project, None)
    except ValueError:
        return WELCOME_NO_PROJECT
    result = tool_flag_question(store_path, question, context)
    if result.get("hint"):
        return f"{result['hint']} The question has still been logged."
    return "Question flagged."


@mcp.tool()
def update_state(
    project: str,
    delta: str,
) -> str:
    """Update current project state with what was completed. Triggers a snapshot.

    Checks for potential contradictions with recent state before writing.

    Args:
        project: Project name.
        delta: Description of what changed (e.g. "Deployed v0.2.0 to staging").
    """
    try:
        store_path = _resolve_store(project, None)
    except ValueError:
        return WELCOME_NO_PROJECT
    result = tool_update_state(store_path, delta)
    if result.get("warning"):
        return f"State updated. {result['warning']}"
    return "State updated."


def _pull_on_startup() -> None:
    """Pull latest from S3 before accepting tool calls.

    Runs synchronously before mcp.run() so the first tool call sees fresh state.
    Never raises — failures are logged and the server starts with local state.
    """
    try:
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        if not config.enabled:
            logger.debug("session-start pull: sync not configured, skipping")
            return
    except Exception as e:
        logger.warning("session-start pull: config load failed: %s", e)
        return

    try:
        project_name = resolve_project(Path(os.getcwd()))
        if not project_name:
            logger.debug("session-start pull: no project found in cwd, skipping")
            return
        store_path = get_store_path(project_name)
        if not store_path.exists():
            logger.debug("session-start pull: store not found for %s, skipping", project_name)
            return

        from nauro.sync.hooks import pull_before_session

        pulled = pull_before_session(project_name, store_path)
        if pulled:
            logger.info("session-start pull: pulled %d file(s) for %s", pulled, project_name)
        else:
            logger.debug("session-start pull: already up to date (%s)", project_name)
    except Exception as e:
        logger.warning("session-start pull: failed, continuing with local state: %s", e)


def run_stdio() -> None:
    """Run the MCP server over stdio (called by `nauro serve --stdio`)."""
    _pull_on_startup()
    mcp.run(transport="stdio")
