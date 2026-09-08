"""Route the existing local decision tool without replacing the full server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import create_model, model_validator

from nauro.mcp.decision_reference import INSTRUCTIONS
from nauro.mcp.resolution_errors import resolution_error_envelope
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import NoProjectError, StoreResolutionError, resolve_project_binding
from nauro.sync.decision_reference_contract import CONTENT, reference_schema, validate_arguments
from nauro.sync.generation_decision import (
    REFUSED_STATUSES,
    DecisionSession,
    select_decision_connection,
)

decision_session = DecisionSession()

REFERENCE_FIELDS = {"request_mode", "operation_id", "payload_digest", "after"}


def _request(arguments: dict[str, Any], project: str) -> dict[str, Any]:
    mode = arguments.get("request_mode") or "prepare"
    fields = CONTENT if mode == "prepare" else REFERENCE_FIELDS
    request = {
        key: value for key, value in arguments.items() if key in fields and value is not None
    }
    request.update(project_id=project, request_mode=mode)
    return request


def generation_proposal(arguments: dict[str, Any]) -> CallToolResult | dict[str, Any] | None:
    try:
        selected = select_decision_connection(arguments.get("project_id"), arguments.get("cwd"))
    except StoreResolutionError as error:
        return resolution_error_envelope(error)
    if selected is None:
        if any(arguments.get(key) is not None for key in REFERENCE_FIELDS):
            raise ValueError("Reference delivery requires a generation-backed project")
        return None
    if arguments.get("request_mode") not in {None, "prepare"}:
        defaults = {"operation": "add"}
        if any(arguments.get(key) != defaults.get(key) for key in CONTENT):
            raise ValueError("Reference-only calls cannot replace content")
    request = _request(arguments, selected[1])
    result = decision_session.execute(selected, request, arguments.get("cwd"))
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        isError=result.get("status") in REFUSED_STATUSES or result.get("unresolved") is True,
    )


def register_argument_validation(server: FastMCP) -> None:
    tool = server._tool_manager.get_tool("propose_decision")
    assert tool is not None

    @model_validator(mode="before")
    def validate_call(cls: Any, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("Decision arguments must be an object")
        try:
            selected = select_decision_connection(value.get("project_id"), value.get("cwd"))
        except StoreResolutionError:
            return value
        if selected is not None:
            raw = {key: item for key, item in value.items() if key not in {"cwd", "mcp_ctx"}}
            raw["project_id"] = selected[1]
            validate_arguments(raw, selected[1])
        elif any(key in value for key in REFERENCE_FIELDS):
            raise ValueError("Reference delivery requires a generation-backed project")
        elif not isinstance(value.get("rationale"), str):
            raise ValueError("Legacy decisions require rationale")
        return value

    tool.fn_metadata.arg_model = create_model(
        "NormalDecisionArguments",
        __base__=tool.fn_metadata.arg_model,
        __validators__={"validate_call": cast(Any, validate_call)},
    )
    schema = reference_schema()
    for key in REFERENCE_FIELDS:
        tool.parameters["properties"][key] = dict(schema["properties"][key])
        tool.parameters["properties"][key].pop("default", None)
    tool.parameters["properties"]["rationale"] = {
        "type": "string",
        "description": tool.parameters["properties"]["rationale"]["description"],
    }
    tool.parameters["properties"]["after"]["description"] = (
        "Opaque cursor; discovery is not a snapshot."
    )
    tool.parameters["oneOf"] = schema["oneOf"]
    tool.description += "\n\nFor generation-backed projects: " + INSTRUCTIONS


def refuse_unadapted_write(project_id: str | None, cwd: str | None) -> dict[str, Any] | None:
    try:
        binding = resolve_project_binding(project_id, cwd or Path.cwd())
    except NoProjectError:
        return None
    except StoreResolutionError as error:
        return resolution_error_envelope(error)
    if observe_generation_marker(binding) is not None:
        raise ValueError("This mutation is not supported for a generation-backed project")
    return None
