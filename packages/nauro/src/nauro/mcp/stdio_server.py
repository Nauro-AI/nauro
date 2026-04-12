"""Nauro MCP server — stdio transport for Claude Code integration.

Spawned by Claude Code at session start, communicates over stdin/stdout.
Same tools as the HTTP server, same store, same payloads.

Tool metadata (descriptions, titles, annotations) is centralized in
`nauro_core.mcp_tools` — edit there, not here — so the local stdio server
and the remote HTTP server stay in sync.

MCP tools (11 total — 7 read, 4 write):
  get_context, get_raw_file, list_decisions, get_decision,
  diff_since_last_session, search_decisions, check_decision,
  propose_decision, confirm_decision, flag_question, update_state
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server import FastMCP
from mcp.types import ToolAnnotations
from nauro_core.constants import MCP_INSTRUCTIONS
from nauro_core.mcp_tools import ToolSpec, get_tool_spec

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
mcp = FastMCP("nauro", instructions=MCP_INSTRUCTIONS, log_level="WARNING")


def _spec_kwargs(name: str) -> dict[str, Any]:
    """Build FastMCP @tool() decorator kwargs from the shared registry."""
    spec: ToolSpec = get_tool_spec(name)
    return {
        "title": spec["title"],
        "description": spec["description"],
        "annotations": ToolAnnotations(**spec["annotations"]),
    }


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


@mcp.tool(**_spec_kwargs("get_context"))
def get_context(
    project: str | None = None,
    cwd: str | None = None,
    level: Literal["L0", "L1", "L2"] | int = "L0",
) -> str:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return WELCOME_NO_PROJECT
    # tool_get_context accepts both int and string levels.
    return tool_get_context(store_path, level)


@mcp.tool(**_spec_kwargs("get_raw_file"))
def get_raw_file(path: str, project: str | None = None, cwd: str | None = None) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_get_raw_file(store_path, path)


@mcp.tool(**_spec_kwargs("list_decisions"))
def list_decisions(
    project: str | None = None,
    cwd: str | None = None,
    limit: int = 20,
    include_superseded: bool = False,
) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_list_decisions(store_path, limit, include_superseded)


@mcp.tool(**_spec_kwargs("get_decision"))
def get_decision(
    number: int,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_get_decision(store_path, number)


@mcp.tool(**_spec_kwargs("diff_since_last_session"))
def diff_since_last_session(
    project: str | None = None,
    cwd: str | None = None,
    days: int | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_diff_since_last_session(store_path, days)


@mcp.tool(**_spec_kwargs("search_decisions"))
def search_decisions(
    query: str,
    limit: int = 10,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_search_decisions(store_path, query, limit)


@mcp.tool(**_spec_kwargs("check_decision"))
def check_decision(
    proposed_approach: str,
    context: str | None = None,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_check_decision(store_path, proposed_approach, context)


@mcp.tool(**_spec_kwargs("propose_decision"))
def propose_decision(
    title: str,
    rationale: str,
    rejected: list[dict] | None = None,
    confidence: str = "medium",
    decision_type: str | None = None,
    reversibility: str | None = None,
    files_affected: list[str] | None = None,
    skip_validation: bool = False,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
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


@mcp.tool(**_spec_kwargs("confirm_decision"))
def confirm_decision(
    confirm_id: str,
    project: str | None = None,
    cwd: str | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    return tool_confirm_decision(store_path, confirm_id)


@mcp.tool(**_spec_kwargs("flag_question"))
def flag_question(
    question: str,
    context: str | None = None,
    project: str | None = None,
    cwd: str | None = None,
) -> str:
    try:
        store_path = _resolve_store(project, cwd)
    except ValueError:
        return WELCOME_NO_PROJECT
    result = tool_flag_question(store_path, question, context)
    if result.get("hint"):
        return f"{result['hint']} The question has still been logged."
    return "Question flagged."


@mcp.tool(**_spec_kwargs("update_state"))
def update_state(
    delta: str,
    project: str | None = None,
    cwd: str | None = None,
) -> str:
    try:
        store_path = _resolve_store(project, cwd)
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
