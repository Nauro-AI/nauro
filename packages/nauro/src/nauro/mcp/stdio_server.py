"""Nauro MCP server — stdio transport for Claude Code integration.

Spawned by Claude Code at session start, communicates over stdin/stdout.
Same store and payloads as the HTTP server.

Tool metadata (descriptions, titles, annotations) is centralized in
`nauro_core.mcp_tools` — edit there, not here — so the local stdio server
and the remote HTTP server stay in sync.

MCP tools registered here (10 — 7 read, 3 write):
  get_context, get_raw_file, list_decisions, get_decision,
  diff_since_last_session, search_decisions, check_decision,
  propose_decision, flag_question, update_state

The shared `nauro_core.mcp_tools.ALL_TOOLS` registry contains 11 tools.
`list_projects` is remote-only — local installs auto-resolve to the
single project store, so the discovery tool is not registered here.

Renderer-scoped read tools listed in ``nauro_core.renderers.RENDERERS``
return a ``CallToolResult`` with a single ``TextContent`` block:
``content[0]`` carries the renderer output. Other tools — write tools,
``get_raw_file``, ``diff_since_last_session`` — keep their existing
single-block shape.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from nauro_core.constants import MCP_INSTRUCTIONS
from nauro_core.mcp_tools import ToolSpec, get_tool_spec
from nauro_core.renderers import RENDERERS as _RENDERERS
from pydantic import Field

from nauro.mcp.tools import (
    tool_check_decision,
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
from nauro.store.registry import (
    get_store_path,
    get_store_path_v2,
    resolve_project,
    resolve_v2_from_path,
)
from nauro.store.resolution import (
    NoProjectError,
    StoreResolutionError,
    resolve_store,
    resolve_via_repo_config,
)

logger = logging.getLogger("nauro.stdio")
mcp = FastMCP("nauro", instructions=MCP_INSTRUCTIONS, log_level="WARNING")


NOT_A_NAURO_REPO = (
    "Not a nauro repo: no .nauro/config.json found. "
    "Run 'nauro init <name>' in this repo, or pass project_id explicitly."
)


def _wrap_with_renderer(
    tool_name: str, result: dict, renderer_kwargs: dict[str, Any] | None = None
) -> CallToolResult:
    """Wrap a renderer-scoped read-tool result in a single ``TextContent`` block.

    Mirrors the remote MCP dispatcher (``mcp_server.mcp_router._build_tool_result``):
    ``content[0]`` carries the renderer output from
    ``nauro_core.renderers.RENDERERS[tool_name]``. A renderer raising — or a
    tool name with no renderer mapped — falls back to a JSON dump of the
    envelope in ``content[0]`` so a presentation bug never swallows a
    response.

    ``renderer_kwargs`` threads renderer-specific options (e.g.
    ``get_decision``'s requested ``mode``) without storing them on the
    result envelope.
    """
    json_text = json.dumps(result, indent=2, default=str)
    renderer = _RENDERERS.get(tool_name)
    if renderer is None:
        return CallToolResult(content=[TextContent(type="text", text=json_text)])
    try:
        rendered = renderer(result, **(renderer_kwargs or {}))
    except Exception:
        logger.exception("renderer failed for tool=%s; falling back to JSON-only", tool_name)
        return CallToolResult(content=[TextContent(type="text", text=json_text)])
    return CallToolResult(content=[TextContent(type="text", text=rendered)])


def _spec_kwargs(name: str) -> dict[str, Any]:
    """Build FastMCP @tool() decorator kwargs from the shared registry."""
    spec: ToolSpec = get_tool_spec(name)
    return {
        "title": spec["title"],
        "description": spec["description"],
        "annotations": ToolAnnotations(**spec["annotations"]),
    }


def _param_desc(tool_name: str, param: str) -> str:
    """Pull a per-property description from the centralized ToolSpec.

    The local FastMCP stdio derives input schemas from Python type hints;
    per-property descriptions are surfaced via
    ``Annotated[T, Field(description=...)]`` rather than passing
    ``input_schema`` through directly. Sourcing the description from the
    ToolSpec at module load time keeps the registry as the single source
    of truth — inlining literal strings here would create a parallel
    surface the drift guards do not cover.
    """
    spec: ToolSpec = get_tool_spec(tool_name)
    props = spec["input_schema"].get("properties", {})
    if param not in props or "description" not in props[param]:
        raise KeyError(
            f"ToolSpec for {tool_name!r} has no description for {param!r}; "
            "either add one in nauro_core.mcp_tools or omit the Annotated."
        )
    return props[param]["description"]


# Back-compat re-exports — tests import these symbols from this module.
# The resolution logic itself lives in nauro.store.resolution.
_resolve_store = resolve_store
_resolve_via_repo_config = resolve_via_repo_config


@mcp.tool(**_spec_kwargs("get_context"))
def get_context(
    project_id: Annotated[
        str | None, Field(description=_param_desc("get_context", "project_id"))
    ] = None,
    cwd: str | None = None,
    level: Annotated[
        Literal["L0", "L1", "L2"] | int,
        Field(description=_param_desc("get_context", "level")),
    ] = "L0",
) -> CallToolResult:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        result = {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        result = {"store": "local", "status": "error", "guidance": str(exc)}
    else:
        # tool_get_context accepts both int and string levels.
        result = tool_get_context(store_path, level)
    return _wrap_with_renderer("get_context", result)


@mcp.tool(**_spec_kwargs("get_raw_file"))
def get_raw_file(
    path: Annotated[str, Field(description=_param_desc("get_raw_file", "path"))],
    project_id: Annotated[
        str | None, Field(description=_param_desc("get_raw_file", "project_id"))
    ] = None,
    cwd: str | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        return {"store": "local", "status": "error", "guidance": str(exc)}
    return tool_get_raw_file(store_path, path)


@mcp.tool(**_spec_kwargs("list_decisions"))
def list_decisions(
    project_id: Annotated[
        str | None, Field(description=_param_desc("list_decisions", "project_id"))
    ] = None,
    cwd: str | None = None,
    limit: Annotated[int, Field(description=_param_desc("list_decisions", "limit"))] = 20,
    include_superseded: Annotated[
        bool, Field(description=_param_desc("list_decisions", "include_superseded"))
    ] = False,
) -> CallToolResult:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        result = {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        result = {"store": "local", "status": "error", "guidance": str(exc)}
    else:
        result = tool_list_decisions(store_path, limit, include_superseded)
    return _wrap_with_renderer("list_decisions", result)


@mcp.tool(**_spec_kwargs("get_decision"))
def get_decision(
    number: Annotated[int, Field(description=_param_desc("get_decision", "number"))],
    mode: Annotated[
        Literal["header", "full"], Field(description=_param_desc("get_decision", "mode"))
    ] = "full",
    project_id: Annotated[
        str | None, Field(description=_param_desc("get_decision", "project_id"))
    ] = None,
    cwd: str | None = None,
) -> CallToolResult:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        result = {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        result = {"store": "local", "status": "error", "guidance": str(exc)}
    else:
        result = tool_get_decision(store_path, number, mode)
    return _wrap_with_renderer("get_decision", result, {"mode": mode})


@mcp.tool(**_spec_kwargs("diff_since_last_session"))
def diff_since_last_session(
    project_id: Annotated[
        str | None,
        Field(description=_param_desc("diff_since_last_session", "project_id")),
    ] = None,
    cwd: str | None = None,
    days: Annotated[
        int | None, Field(description=_param_desc("diff_since_last_session", "days"))
    ] = None,
) -> dict:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        return {"store": "local", "status": "error", "guidance": str(exc)}
    return tool_diff_since_last_session(store_path, days)


@mcp.tool(**_spec_kwargs("search_decisions"))
def search_decisions(
    query: Annotated[str, Field(description=_param_desc("search_decisions", "query"))],
    limit: Annotated[int, Field(description=_param_desc("search_decisions", "limit"))] = 10,
    include_superseded: Annotated[
        bool, Field(description=_param_desc("search_decisions", "include_superseded"))
    ] = False,
    project_id: Annotated[
        str | None, Field(description=_param_desc("search_decisions", "project_id"))
    ] = None,
    cwd: str | None = None,
) -> CallToolResult:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        result = {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        result = {"store": "local", "status": "error", "guidance": str(exc)}
    else:
        result = tool_search_decisions(store_path, query, limit, include_superseded)
    return _wrap_with_renderer("search_decisions", result)


@mcp.tool(**_spec_kwargs("check_decision"))
def check_decision(
    proposed_approach: Annotated[
        str, Field(description=_param_desc("check_decision", "proposed_approach"))
    ],
    context: Annotated[
        str | None, Field(description=_param_desc("check_decision", "context"))
    ] = None,
    project_id: Annotated[
        str | None, Field(description=_param_desc("check_decision", "project_id"))
    ] = None,
    cwd: str | None = None,
) -> CallToolResult:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        result = {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        result = {"store": "local", "status": "error", "guidance": str(exc)}
    else:
        result = tool_check_decision(store_path, proposed_approach, context)
    return _wrap_with_renderer("check_decision", result)


@mcp.tool(**_spec_kwargs("propose_decision"))
def propose_decision(
    title: Annotated[str, Field(description=_param_desc("propose_decision", "title"))],
    rationale: Annotated[str, Field(description=_param_desc("propose_decision", "rationale"))],
    operation: Annotated[
        Literal["add", "update", "supersede"],
        Field(description=_param_desc("propose_decision", "operation")),
    ] = "add",
    affected_decision_id: Annotated[
        str | None,
        Field(description=_param_desc("propose_decision", "affected_decision_id")),
    ] = None,
    rejected: Annotated[
        list[dict] | None,
        Field(description=_param_desc("propose_decision", "rejected")),
    ] = None,
    confidence: Annotated[
        Literal["high", "medium", "low"] | None,
        Field(description=_param_desc("propose_decision", "confidence")),
    ] = None,
    decision_type: Annotated[
        Literal[
            "architecture",
            "library_choice",
            "pattern",
            "refactor",
            "api_design",
            "infrastructure",
            "data_model",
        ]
        | None,
        Field(description=_param_desc("propose_decision", "decision_type")),
    ] = None,
    reversibility: Annotated[
        Literal["easy", "moderate", "hard"] | None,
        Field(description=_param_desc("propose_decision", "reversibility")),
    ] = None,
    files_affected: Annotated[
        list[str] | None,
        Field(description=_param_desc("propose_decision", "files_affected")),
    ] = None,
    resolves_questions: Annotated[
        list[str] | None,
        Field(description=_param_desc("propose_decision", "resolves_questions")),
    ] = None,
    project_id: Annotated[
        str | None,
        Field(description=_param_desc("propose_decision", "project_id")),
    ] = None,
    cwd: str | None = None,
) -> dict:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        return {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except StoreResolutionError as exc:
        return {"store": "local", "status": "error", "guidance": str(exc)}
    return tool_propose_decision(
        store_path,
        title=title,
        rationale=rationale,
        operation=operation,
        affected_decision_id=affected_decision_id,
        rejected=rejected,
        confidence=confidence,
        decision_type=decision_type,
        reversibility=reversibility,
        files_affected=files_affected,
        resolves_questions=resolves_questions,
    )


@mcp.tool(**_spec_kwargs("flag_question"))
def flag_question(
    question: Annotated[
        str | None, Field(description=_param_desc("flag_question", "question"))
    ] = None,
    context: Annotated[
        str | None, Field(description=_param_desc("flag_question", "context"))
    ] = None,
    targets: Annotated[
        list[str] | None, Field(description=_param_desc("flag_question", "targets"))
    ] = None,
    resolved_by: Annotated[
        str | None, Field(description=_param_desc("flag_question", "resolved_by"))
    ] = None,
    project_id: Annotated[
        str | None, Field(description=_param_desc("flag_question", "project_id"))
    ] = None,
    cwd: str | None = None,
) -> str:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        return WELCOME_NO_PROJECT
    except StoreResolutionError as exc:
        return str(exc)
    result = tool_flag_question(
        store_path, question, context, targets=targets, resolved_by=resolved_by
    )
    if result.get("status") == "rejected":
        error = result.get("error") or {}
        reason = error.get("reason", "Flag rejected.")
        return reason
    if resolved_by is not None:
        return "Question(s) resolved."
    if result.get("hint"):
        return f"{result['hint']} The question has still been logged."
    return "Question flagged."


@mcp.tool(**_spec_kwargs("update_state"))
def update_state(
    delta: Annotated[str, Field(description=_param_desc("update_state", "delta"))],
    project_id: Annotated[
        str | None, Field(description=_param_desc("update_state", "project_id"))
    ] = None,
    cwd: str | None = None,
) -> str:
    try:
        store_path = _resolve_store(project_id, cwd)
    except NoProjectError:
        return WELCOME_NO_PROJECT
    except StoreResolutionError as exc:
        return str(exc)
    result = tool_update_state(store_path, delta)
    if result.get("warning"):
        return f"State updated. {result['warning']}"
    return "State updated."


def _pull_on_startup() -> None:
    """Pull latest from remote before accepting tool calls.

    Runs synchronously before mcp.run() so the first tool call sees fresh state.
    Auth and cloud-mode gating happen inside ``pull_before_session`` — here we
    only resolve the project key from cwd. Never raises — failures are logged
    and the server starts with local state.
    """
    try:
        cwd = Path(os.getcwd())
        project_key: str | None = None
        store_path: Path | None = None

        via_config = _resolve_via_repo_config(cwd)
        if via_config is not None:
            project_key, store_path = via_config
        else:
            v2_match = resolve_v2_from_path(cwd)
            if v2_match is not None:
                pid, _entry = v2_match
                project_key, store_path = pid, get_store_path_v2(pid)
            else:
                legacy_name = resolve_project(cwd)
                if legacy_name:
                    project_key, store_path = legacy_name, get_store_path(legacy_name)

        if not project_key or store_path is None:
            logger.debug("session-start pull: no project found in cwd, skipping")
            return
        if not store_path.exists():
            logger.debug("session-start pull: store not found for %s, skipping", project_key)
            return

        from nauro.sync.hooks import pull_before_session

        pulled = pull_before_session(project_key, store_path)
        if pulled:
            logger.info("session-start pull: pulled %d file(s) for %s", pulled, project_key)
        else:
            logger.debug("session-start pull: nothing to do for %s", project_key)
    except Exception as e:
        logger.warning("session-start pull: failed, continuing with local state: %s", e)


def run_stdio() -> None:
    """Run the MCP server over stdio (called by `nauro serve --stdio`)."""
    from nauro.telemetry.transport import set_transport

    set_transport("stdio")
    _pull_on_startup()
    mcp.run(transport="stdio")
