"""Explicit reference binding for the existing decision tool in isolated tests."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import create_model, model_validator

from nauro.mcp.reference_reads import register_reference_reads
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.decision_reference_contract import reference_schema, validate_arguments

INSTRUCTIONS = (
    "Prepare an immutable decision with the existing propose_decision tool. Preparation does "
    "not commit. Present the complete effective_draft and its base, end the turn, and obtain "
    "explicit approval in the owner's next reply. Submit only its saved operation_id and "
    "payload_digest. Recover is lookup only. Discover finds requests without a remembered "
    "reference; display ambiguous drafts for explicit selection. Never prepare a replacement "
    "to recover a lost response. Stale requires fresh preparation and approval. Retry is an "
    "explicit separate action using the original identity and deadline."
)


def bind_decision_reference(server: FastMCP, transport: DecisionReferenceTransport) -> None:
    """Replace only propose_decision on an explicitly supplied server instance."""
    if server._tool_manager.get_tool("propose_decision") is None:
        raise ValueError("The existing decision tool must be registered first")
    server.remove_tool("propose_decision")
    _register_decision_reference(server, transport)


def reference_server(transport: DecisionReferenceTransport) -> FastMCP:
    from nauro import __version__

    server = FastMCP(
        "nauro",
        instructions=f"{INSTRUCTIONS} Project ID: {transport.project}.",
        log_level="WARNING",
    )
    server._mcp_server.version = __version__
    _register_decision_reference(server, transport)
    register_reference_reads(server, transport)
    return server


def _register_decision_reference(server: FastMCP, transport: DecisionReferenceTransport) -> None:

    def propose_decision(
        project_id: str,
        request_mode: Literal["prepare", "submit", "recover", "discover", "retry"] = "prepare",
        operation_id: str | None = None,
        payload_digest: str | None = None,
        after: str | None = None,
        title: str | None = None,
        rationale: str | None = None,
        operation: Literal["add", "update", "supersede"] | None = None,
        affected_decision_id: str | None = None,
        rejected: list[dict[str, Any]] | None = None,
        confidence: Literal["high", "medium", "low"] | None = None,
        decision_type: str | None = None,
        reversibility: str | None = None,
        files_affected: list[str] | None = None,
        resolves_questions: list[str] | None = None,
    ) -> CallToolResult:
        arguments = {
            key: value
            for key, value in locals().items()
            if value is not None and key != "transport"
        }
        result = transport.propose_decision(**arguments)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            isError=result.get("status")
            in {"stale", "unresolved", "pending", "expired", "conflict", "disposed"},
        )

    server.add_tool(
        propose_decision,
        name="propose_decision",
        description=INSTRUCTIONS,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
        structured_output=False,
    )

    tool = server._tool_manager.get_tool("propose_decision")
    assert tool is not None

    @model_validator(mode="before")
    def validate_call(cls: Any, value: Any) -> Any:
        validate_arguments(value, transport.project)
        return value

    tool.fn_metadata.arg_model = create_model(
        "DecisionReferenceArguments",
        __base__=tool.fn_metadata.arg_model,
        __validators__={"validate_call": cast(Any, validate_call)},
    )

    tool.parameters = reference_schema()
