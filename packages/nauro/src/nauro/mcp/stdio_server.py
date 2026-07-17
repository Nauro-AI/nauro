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
from nauro_core.operations import ErrorPayload
from nauro_core.renderers import RENDERERS as _RENDERERS
from nauro_core.renderers import disconnected_reason_code
from pydantic import Field

from nauro import __version__
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
from nauro.store.resolution import (
    DisconnectedProject,
    DisconnectedProjectError,
    NoProjectError,
    RepoResolution,
    StoreResolutionError,
    resolve_from_cwd,
    resolve_store,
)

logger = logging.getLogger("nauro.stdio")
mcp = FastMCP("nauro", instructions=MCP_INSTRUCTIONS, log_level="WARNING")
# FastMCP does not forward a version to the underlying low-level server, which
# otherwise reports the mcp framework version in the initialize response. Set
# the nauro package version so connecting clients see the release in use.
mcp._mcp_server.version = __version__


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
    result envelope. Disconnected-project errors also retain their existing
    structured envelope so clients can act on stable recovery metadata while
    humans still receive the rendered guidance.
    """
    json_text = json.dumps(result, indent=2, default=str)
    structured_content = result if disconnected_reason_code(result) is not None else None
    renderer = _RENDERERS.get(tool_name)
    if renderer is None:
        return CallToolResult(
            content=[TextContent(type="text", text=json_text)],
            structuredContent=structured_content,
        )
    try:
        rendered = renderer(result, **(renderer_kwargs or {}))
    except Exception:
        logger.exception("renderer failed for tool=%s; falling back to JSON-only", tool_name)
        return CallToolResult(
            content=[TextContent(type="text", text=json_text)],
            structuredContent=structured_content,
        )
    return CallToolResult(
        content=[TextContent(type="text", text=rendered)],
        structuredContent=structured_content,
    )


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


# Back-compat re-export — tests import this symbol from this module.
# The resolution logic itself lives in nauro.store.resolution.
_resolve_store = resolve_store


def _resolve_or_error(project_id, cwd) -> tuple[Path | None, dict | None]:
    """Resolve a store path, or translate a resolution failure to an error dict.

    Every tool wrapper shares this preamble: on success it returns
    ``(store_path, None)``; on failure it returns ``(None, error_dict)`` where
    the dict carries the transport-appropriate guidance — the welcome screen for
    the genuine no-project case, the specific diagnostic otherwise. Each wrapper
    routes the dict to its own output shape (renderer envelope, dict, or the
    guidance string).
    """
    try:
        return _resolve_store(project_id, cwd), None
    except NoProjectError:
        return None, {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
    except DisconnectedProjectError as exc:
        state = exc.state
        return None, {
            "store": "local",
            "status": "error",
            "error": ErrorPayload(kind="error", reason=state.guidance).model_dump(
                exclude_none=True
            ),
            "guidance": state.guidance,
            "project_id": state.project_id,
            "project_name": state.display_name,
            "project_mode": state.mode,
            "reason_code": state.reason_code,
            "recovery_actions": list(state.recovery_actions),
        }
    except StoreResolutionError as exc:
        return None, {"store": "local", "status": "error", "guidance": str(exc)}


# ``cwd`` exists only on the local transport (the hosted server has no
# filesystem to resolve against), so its description lives here rather than
# in the shared ToolSpec registry, which would surface it on remote schemas.
_CWD_PARAM = Annotated[
    str | None,
    Field(
        description=(
            "Optional. The caller's absolute working directory; resolves "
            "the project from the local registry when project_id is omitted."
        )
    ),
]


@mcp.tool(**_spec_kwargs("get_context"))
def get_context(
    project_id: Annotated[
        str | None, Field(description=_param_desc("get_context", "project_id"))
    ] = None,
    cwd: _CWD_PARAM = None,
    level: Annotated[
        Literal["L0", "L1", "L2"] | int,
        Field(description=_param_desc("get_context", "level")),
    ] = "L0",
) -> CallToolResult:
    store_path, err = _resolve_or_error(project_id, cwd)
    # tool_get_context accepts both int and string levels.
    result = err if err is not None else tool_get_context(store_path, level)
    return _wrap_with_renderer("get_context", result)


@mcp.tool(**_spec_kwargs("get_raw_file"))
def get_raw_file(
    path: Annotated[str, Field(description=_param_desc("get_raw_file", "path"))],
    project_id: Annotated[
        str | None, Field(description=_param_desc("get_raw_file", "project_id"))
    ] = None,
    cwd: _CWD_PARAM = None,
) -> dict:
    store_path, err = _resolve_or_error(project_id, cwd)
    if err is not None:
        return err
    return tool_get_raw_file(store_path, path)


@mcp.tool(**_spec_kwargs("list_decisions"))
def list_decisions(
    project_id: Annotated[
        str | None, Field(description=_param_desc("list_decisions", "project_id"))
    ] = None,
    cwd: _CWD_PARAM = None,
    limit: Annotated[int, Field(description=_param_desc("list_decisions", "limit"))] = 20,
    include_superseded: Annotated[
        bool, Field(description=_param_desc("list_decisions", "include_superseded"))
    ] = False,
) -> CallToolResult:
    store_path, err = _resolve_or_error(project_id, cwd)
    result = err if err is not None else tool_list_decisions(store_path, limit, include_superseded)
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
    cwd: _CWD_PARAM = None,
) -> CallToolResult:
    store_path, err = _resolve_or_error(project_id, cwd)
    result = err if err is not None else tool_get_decision(store_path, number, mode)
    return _wrap_with_renderer("get_decision", result, {"mode": mode})


@mcp.tool(**_spec_kwargs("diff_since_last_session"))
def diff_since_last_session(
    project_id: Annotated[
        str | None,
        Field(description=_param_desc("diff_since_last_session", "project_id")),
    ] = None,
    cwd: _CWD_PARAM = None,
    days: Annotated[
        int | None, Field(description=_param_desc("diff_since_last_session", "days"))
    ] = None,
) -> dict:
    store_path, err = _resolve_or_error(project_id, cwd)
    if err is not None:
        return err
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
    cwd: _CWD_PARAM = None,
) -> CallToolResult:
    store_path, err = _resolve_or_error(project_id, cwd)
    result = (
        err
        if err is not None
        else tool_search_decisions(store_path, query, limit, include_superseded)
    )
    # The kernel envelope omits the echoed query; thread it to the renderer
    # so the local header shows the term, matching the remote transport,
    # which carries query in its envelope.
    return _wrap_with_renderer("search_decisions", result, {"query": query})


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
    cwd: _CWD_PARAM = None,
) -> CallToolResult:
    store_path, err = _resolve_or_error(project_id, cwd)
    result = err if err is not None else tool_check_decision(store_path, proposed_approach, context)
    return _wrap_with_renderer("check_decision", result)


@mcp.tool(**_spec_kwargs("propose_decision"))
def propose_decision(
    rationale: Annotated[str, Field(description=_param_desc("propose_decision", "rationale"))],
    title: Annotated[str, Field(description=_param_desc("propose_decision", "title"))] = "",
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
        # A Literal is a static annotation and cannot be built from the runtime
        # DECISION_TYPE_VALUES tuple, so these are hand-written. The drift-guard
        # test in test_stdio_server.py asserts this set equals the enum values.
        Literal[
            "architecture",
            "api_design",
            "infrastructure",
            "pattern",
            "refactor",
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
    cwd: _CWD_PARAM = None,
) -> dict:
    store_path, err = _resolve_or_error(project_id, cwd)
    if err is not None:
        return err
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
    cwd: _CWD_PARAM = None,
) -> str | dict:
    store_path, err = _resolve_or_error(project_id, cwd)
    if err is not None:
        return err if disconnected_reason_code(err) is not None else err["guidance"]
    result = tool_flag_question(
        store_path, question, context, targets=targets, resolved_by=resolved_by
    )
    if result.get("status") == "rejected":
        error = result.get("error") or {}
        reason = str(error.get("reason", "Flag rejected."))
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
    cwd: _CWD_PARAM = None,
) -> str | dict:
    store_path, err = _resolve_or_error(project_id, cwd)
    if err is not None:
        return err if disconnected_reason_code(err) is not None else err["guidance"]
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
        resolution = resolve_from_cwd(Path(os.getcwd()))
        if resolution is None or isinstance(resolution, DisconnectedProject):
            logger.debug("session-start pull: no project found in cwd, skipping")
            return
        assert isinstance(resolution, RepoResolution)
        project_key, store_path = resolution.project_id, resolution.store_path
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
    _pull_on_startup()
    mcp.run(transport="stdio")
