"""Closed registry and read-result contracts for the reference connection."""

from __future__ import annotations

import copy
from typing import Any

from nauro_core.mcp_tools import GET_CONTEXT, GET_DECISION

from nauro.sync.decision_reference_contract import (
    MAX_RESPONSE,
    DecisionReferenceError,
    reference_schema,
)

READ_SPECS = {spec["name"]: spec for spec in (GET_CONTEXT, GET_DECISION)}


def read_schema(name: str) -> dict[str, Any]:
    schema = copy.deepcopy(READ_SPECS[name]["input_schema"])
    schema["required"] = [*schema.get("required", []), "project_id"]
    schema["additionalProperties"] = False
    return schema


def negotiate_registry(result: Any) -> bool:
    if (
        not isinstance(result, dict)
        or set(result) != {"tools"}
        or not isinstance(result["tools"], list)
    ):
        raise DecisionReferenceError("Unexpected isolated tool registry")
    schemas = {"propose_decision": reference_schema()}
    names: set[str] = set()
    tools = result["tools"]
    if len(tools) == 3:
        schemas.update({name: read_schema(name) for name in READ_SPECS})
    if len(tools) != len(schemas):
        raise DecisionReferenceError("Unexpected isolated tool registry")
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise DecisionReferenceError("Unexpected isolated tool registry")
        name = tool["name"]
        if name in names or name not in schemas or tool.get("inputSchema") != schemas[name]:
            raise DecisionReferenceError("Unexpected isolated tool registry")
        names.add(name)
    return len(tools) == 3


def validate_read(name: str, arguments: Any, project: str) -> None:
    if name not in READ_SPECS or not isinstance(arguments, dict):
        raise DecisionReferenceError("Invalid reference read")
    schema = read_schema(name)
    if (
        set(arguments) - set(schema["properties"])
        or set(schema["required"]) - set(arguments)
        or arguments.get("project_id") != project
    ):
        raise DecisionReferenceError(
            "Read must select the configured project with supported fields"
        )
    if name == "get_context":
        level = arguments.get("level", "L0")
        if not isinstance(level, str) or level not in {"L0", "L1", "L2"}:
            raise DecisionReferenceError("Invalid context level")
    else:
        mode = arguments.get("mode", "full")
        if (
            type(arguments["number"]) is not int
            or not isinstance(mode, str)
            or mode not in {"header", "full"}
        ):
            raise DecisionReferenceError("Invalid decision number or mode")


def verify_read_result(result: Any) -> dict[str, Any]:
    if (
        not isinstance(result, dict)
        or set(result) != {"content", "isError"}
        or type(result["isError"]) is not bool
    ):
        raise DecisionReferenceError("Invalid reference read result")
    content = result["content"]
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise DecisionReferenceError("Invalid reference read content")
    text = content[0]
    if (
        set(text) != {"type", "text"}
        or text["type"] != "text"
        or not isinstance(text["text"], str)
        or len(text["text"].encode("utf-8")) > MAX_RESPONSE
    ):
        raise DecisionReferenceError("Invalid reference read text")
    return result
