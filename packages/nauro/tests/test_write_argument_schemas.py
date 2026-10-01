"""Published write modes reject incomplete submissions before transport setup."""

import asyncio

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from nauro.mcp.stdio_server import mcp

REFERENCE = {"operation_id": "a" * 32, "payload_digest": "b" * 64}
PAYLOADS = {"update_state": {"delta": "Current work"}}


@pytest.mark.parametrize("name", PAYLOADS)
@pytest.mark.parametrize("mode", [None, "submit", "discover", "recover", "retry"])
def test_valid_modes_match_schema_and_runtime(name, mode):
    arguments = dict(PAYLOADS[name]) if mode in {None, "submit"} else {}
    if mode is not None:
        arguments["request_mode"] = mode
    if mode in {"recover", "retry"}:
        arguments.update(REFERENCE)
    tool = mcp._tool_manager.get_tool(name)
    Draft202012Validator(tool.parameters).validate(arguments)
    tool.fn_metadata.arg_model.model_validate(arguments)


@pytest.mark.parametrize("name", PAYLOADS)
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"request_mode": "submit"},
        {"request_mode": "recover"},
        {"request_mode": "retry", "operation_id": "a" * 32},
        {"request_mode": "discover", **REFERENCE},
    ],
)
def test_invalid_modes_fail_schema_and_runtime(name, arguments):
    tool = mcp._tool_manager.get_tool(name)
    assert list(Draft202012Validator(tool.parameters).iter_errors(arguments))
    with pytest.raises(ValidationError):
        tool.fn_metadata.arg_model.model_validate(arguments)


@pytest.mark.parametrize("name", PAYLOADS)
@pytest.mark.parametrize("mode", ["discover", "recover", "retry"])
def test_reference_modes_reject_replacement_payload(name, mode):
    arguments = {**PAYLOADS[name], "request_mode": mode}
    if mode != "discover":
        arguments.update(REFERENCE)
    tool = mcp._tool_manager.get_tool(name)
    assert list(Draft202012Validator(tool.parameters).iter_errors(arguments))
    with pytest.raises(ValidationError, match="content"):
        tool.fn_metadata.arg_model.model_validate(arguments)


@pytest.mark.parametrize("name", PAYLOADS)
def test_missing_submit_fields_never_reach_transport(monkeypatch, name):
    from mcp.server.fastmcp.exceptions import ToolError

    from nauro.sync import generation_writes

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid input reached the write dispatcher")

    monkeypatch.setattr(generation_writes, "generation_write", forbidden)
    with pytest.raises(ToolError, match="requires"):
        asyncio.run(mcp._tool_manager.get_tool(name).run({}))


def test_registered_state_keeps_literal_null_payload(monkeypatch):
    from nauro.sync import generation_writes

    captured = []

    def submit(operation, arguments, **kwargs):
        captured.append(arguments)
        return {"status": "committed"}

    monkeypatch.setattr(generation_writes, "generation_write", submit)
    asyncio.run(mcp._tool_manager.get_tool("update_state").run({"delta": "null"}))
    assert captured[0]["delta"] == "null"


@pytest.mark.parametrize(
    "arguments",
    [
        {"delta": "Changed", "expected_revision": "null"},
        {"request_mode": "recover", **REFERENCE, "delta": "null"},
    ],
)
def test_registered_state_rejects_literal_null_as_revision_or_replacement(monkeypatch, arguments):
    from mcp.server.fastmcp.exceptions import ToolError

    from nauro.sync import generation_writes

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid scalar reached the dispatcher")

    monkeypatch.setattr(generation_writes, "generation_write", forbidden)
    with pytest.raises(ToolError):
        asyncio.run(mcp._tool_manager.get_tool("update_state").run(arguments))


def test_state_modes_preserve_registered_tool_inventory():
    assert {tool.name for tool in mcp._tool_manager.list_tools()} == {
        "get_context",
        "propose_decision",
        "check_decision",
        "flag_question",
        "update_state",
        "search_decisions",
        "get_raw_file",
        "list_decisions",
        "get_decision",
        "diff_since_last_session",
    }
