"""Register only the negotiated read pair in an isolated reference server."""

from __future__ import annotations

from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import create_model, model_validator

from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.reference_reads import READ_SPECS, read_schema, validate_read


def _register(server: FastMCP, transport: DecisionReferenceTransport, name: str) -> None:
    def result(arguments: dict[str, Any]) -> CallToolResult:
        response = transport.read(name, **arguments)
        return CallToolResult(
            content=[TextContent(type="text", text=response["content"][0]["text"])],
            isError=response["isError"],
        )

    def get_context(project_id: str, level: Literal["L0", "L1", "L2"] = "L0") -> CallToolResult:
        return result({"project_id": project_id, "level": level})

    def get_decision(
        project_id: str,
        number: int,
        mode: Literal["header", "full"] = "full",
    ) -> CallToolResult:
        return result({"project_id": project_id, "number": number, "mode": mode})

    spec = READ_SPECS[name]
    server.add_tool(
        get_context if name == "get_context" else get_decision,
        name=name,
        title=spec["title"],
        description=spec["description"],
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
        structured_output=False,
    )
    tool = server._tool_manager.get_tool(name)
    assert tool is not None

    @model_validator(mode="before")
    def validate_call(cls: Any, value: Any) -> Any:
        validate_read(name, value, transport.project)
        return value

    tool.fn_metadata.arg_model = create_model(
        "ReferenceReadArguments",
        __base__=tool.fn_metadata.arg_model,
        __validators__={"validate_call": cast(Any, validate_call)},
    )
    tool.parameters = read_schema(name)


def register_reference_reads(server: FastMCP, transport: DecisionReferenceTransport) -> None:
    if transport.reads_available:
        for name in READ_SPECS:
            _register(server, transport, name)
