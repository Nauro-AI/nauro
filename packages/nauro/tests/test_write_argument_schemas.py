"""Published write modes reject incomplete submissions before transport setup."""

import asyncio

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from nauro.mcp.stdio_server import mcp

REFERENCE = {"operation_id": "a" * 32, "payload_digest": "b" * 64}
PAYLOADS = {
    "update_state": {"delta": "Current work"},
    "flag_question": {"question": "Which option?"},
    "update_stack": {"content": "Python"},
    "share_context": {
        "slug": "brief",
        "content": "Details",
        "pointer_kind": "brief",
        "summary": "Summary",
    },
}


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


@pytest.mark.parametrize("name", ["update_stack", "share_context"])
def test_contract_descriptions_reach_agent(name):
    from nauro_core.mcp_tools import SHARE_CONTEXT, UPDATE_STACK

    spec = UPDATE_STACK if name == "update_stack" else SHARE_CONTEXT
    properties = mcp._tool_manager.get_tool(name).parameters["properties"]
    for parameter, schema in spec["input_schema"]["properties"].items():
        if parameter != "operation_id":
            assert properties[parameter]["description"] == schema["description"]
    for parameter in ("request_mode", "operation_id", "payload_digest"):
        assert properties[parameter]["description"]


@pytest.mark.parametrize("name", ["update_stack", "share_context"])
def test_project_resolution_error_keeps_specific_guidance(tmp_path, monkeypatch, name):
    from nauro.mcp import stdio_server

    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    result = getattr(stdio_server, name)(project_id="missing-project", **PAYLOADS[name])
    assert result["status"] == "error"
    assert "missing-project" in result["guidance"]


@pytest.mark.parametrize("name", PAYLOADS)
def test_missing_submit_fields_never_reach_transport(monkeypatch, name):
    from mcp.server.fastmcp.exceptions import ToolError

    from nauro.sync import generation_writes

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid input reached the write dispatcher")

    monkeypatch.setattr(generation_writes, "generation_write", forbidden)
    with pytest.raises(ToolError, match="requires"):
        asyncio.run(mcp._tool_manager.get_tool(name).run({}))


@pytest.mark.parametrize("missing", PAYLOADS["share_context"])
def test_share_submit_requires_each_field(missing):
    arguments = dict(PAYLOADS["share_context"])
    arguments.pop(missing)
    tool = mcp._tool_manager.get_tool("share_context")
    assert list(Draft202012Validator(tool.parameters).iter_errors(arguments))
    with pytest.raises(ValidationError, match=missing):
        tool.fn_metadata.arg_model.model_validate(arguments)


@pytest.mark.parametrize(
    "arguments,valid",
    [
        ({"resolved_by": "D42", "targets": ["Q1"]}, True),
        ({"question": "Which?", "resolved_by": "D42"}, False),
        ({"resolved_by": "D42", "context": "Replacement"}, False),
        ({"request_mode": ""}, False),
    ],
)
def test_question_modes_match_advertised_contract(arguments, valid):
    tool = mcp._tool_manager.get_tool("flag_question")
    assert bool(list(Draft202012Validator(tool.parameters).iter_errors(arguments))) is not valid
    if valid:
        tool.fn_metadata.arg_model.model_validate(arguments)
    else:
        with pytest.raises(ValidationError):
            tool.fn_metadata.arg_model.model_validate(arguments)
