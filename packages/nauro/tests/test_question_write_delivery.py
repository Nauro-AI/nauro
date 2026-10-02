"""Question surfaces preserve saved writes and validate mode-specific content."""

import asyncio
import json

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.stdio_server import mcp
from nauro.store.question_records import list_question_submissions
from nauro.sync import generation_writes as writes
from tests.test_generation_write_delivery import ACTOR, PROJECT, delivery

__all__ = ["delivery"]
CONTENT = [{"question": "Next?", "context": "null"}, {"resolved_by": "D42", "targets": ["Q1"]}]


def invoke(surface, arguments):
    if surface == "stdio":
        result = asyncio.run(mcp._tool_manager.get_tool("flag_question").run(arguments))
        return json.loads(result.content[0].text) if hasattr(result, "isError") else result
    command = ["flag-question"]
    for key, value in arguments.items():
        for item in value if isinstance(value, list) else [value]:
            command.extend(["--" + key.replace("_", "-"), item])
    result = CliRunner().invoke(app, command)
    payload = json.loads(result.stdout)
    expected_exit = int(
        payload.get("unresolved") or payload.get("status") not in {"committed", "discovered"}
    )
    assert result.exit_code == expected_exit, result.output
    return payload


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("content", CONTENT)
def test_saved_reference_recovery_and_absent_retry_keep_original_content(
    delivery, surface, content
):
    session, calls, behavior = delivery
    behavior["drop"] = True
    result = invoke(surface, content)
    assert result["status"] == "unresolved"
    reference = {key: result[key] for key in ("operation_id", "payload_digest")}
    (original,) = list_question_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    behavior.update(drop=False, status="absent")
    assert invoke(surface, {"request_mode": "recover", **reference})["status"] == "absent"
    invoke(surface, {"request_mode": "retry", **reference})
    assert [call.url.path for call in calls] == [
        "/questions/submit",
        "/questions/lookup",
        "/questions/lookup",
        "/questions/submit",
    ]
    assert [json.loads(call.content)["payload_json"] for call in calls] == [
        original.payload_json
    ] * 4
    (saved,) = list_question_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert saved.created_at == original.created_at
    assert saved.scope == original.scope
    session.connection = session.connection.model_copy(update={"client_id": "other-client"})
    assert invoke(surface, {"request_mode": "discover"}) == {"status": "discovered", "attempts": []}
    with pytest.raises(ValueError, match="connection"):
        writes.generation_write("flag_question", {"request_mode": "recover", **reference})
    assert len(calls) == 4


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("content", CONTENT)
def test_question_commit_keeps_receipt_when_refresh_fails(delivery, surface, content):
    _, calls, _ = delivery
    writes.refresh_replica.side_effect = ValueError("offline")
    result = invoke(surface, content)
    assert result["status"] == "committed"
    assert result["replica_status"]["error_code"] == "receipt_refresh_required"
    reference = {key: result[key] for key in ("operation_id", "payload_digest")}
    assert (
        invoke(surface, {"request_mode": "retry", **reference})["receipt_json"]
        == result["receipt_json"]
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"question": "null", "context": "null", "targets": '["Q1"]'},
        {"resolved_by": "D42", "targets": '["Q1"]'},
    ],
)
def test_registered_question_keeps_scalar_strings_and_encoded_targets(delivery, arguments):
    session, calls, _ = delivery
    assert invoke("stdio", arguments)["status"] == "committed"
    (saved,) = list_question_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    payload = json.loads(saved.payload_json)
    assert payload["targets"] == ["Q1"]
    if "question" in arguments:
        assert payload["question"] == "null"
        assert payload["context"] == "null"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"question": "Next?", "resolved_by": "D42"},
        {"resolved_by": "D42", "context": "context"},
        {"request_mode": "null", "question": "Next?"},
        {"request_mode": "discover", "question": "null"},
        {
            "request_mode": "recover",
            "operation_id": "a" * 32,
            "payload_digest": "b" * 64,
            "resolved_by": "null",
        },
    ],
)
def test_invalid_question_mode_fails_schema_and_sdk_before_io(delivery, arguments):
    _, calls, _ = delivery
    tool = mcp._tool_manager.get_tool("flag_question")
    assert list(Draft202012Validator(tool.parameters).iter_errors(arguments))
    with pytest.raises(ValidationError):
        tool.fn_metadata.arg_model.model_validate(arguments)
    with pytest.raises(ToolError):
        asyncio.run(tool.run(arguments))
    assert calls == []


@pytest.mark.parametrize(
    "arguments", [{"resolved_by": "D42", "targets": ["Q1"]}, {"question": "Next?"}]
)
def test_question_submit_schema_accepts_both_actions(arguments):
    tool = mcp._tool_manager.get_tool("flag_question")
    Draft202012Validator(tool.parameters).validate(arguments)
    tool.fn_metadata.arg_model.model_validate(arguments)


def test_cli_cannot_hide_duplicate_or_reference_question_alias(delivery):
    _, calls, _ = delivery
    for arguments in [
        ["flag-question", "Next?", "--question", "Other?"],
        ["flag-question", "--request-mode", "discover", "--question", "Next?"],
    ]:
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("mode", ["submit", "recover"])
def test_question_refusal_preserves_reference_and_certainty(delivery, surface, mode):
    session, _, behavior = delivery
    arguments = {"question": "Next?"}
    if mode == "recover":
        behavior["drop"] = True
        result = invoke(surface, arguments)
        arguments = {
            "request_mode": mode,
            **{key: result[key] for key in ("operation_id", "payload_digest")},
        }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"detail": "project_not_selected"})
        )
    ) as client:
        session.client = client
        result = invoke(surface, arguments)
    (saved,) = list_question_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert result["status"] == "refused"
    assert result["server_code"] == result["error_code"] == "project_not_selected"
    assert result["unresolved"] is (mode == "recover")
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    assert ("outcome remains unknown" if mode == "recover" else "attempt did not write") in result[
        "guidance"
    ]
    writes.refresh_replica.assert_not_called()


def test_question_actor_change_cannot_prepare_or_send(delivery):
    session, calls, _ = delivery
    session.require_actor.side_effect = ValueError("account changed")
    with pytest.raises(ValueError, match="account changed"):
        writes.generation_write("flag_question", {"question": "Next?"})
    assert calls == []


def test_question_description_only_advertises_question_options():
    description = mcp._tool_manager.get_tool("flag_question").description
    assert "expected_revision" not in description
    assert "State submissions" not in description
    assert "Recover only looks up" in description


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("content", CONTENT)
def test_transport_constructor_refusal_keeps_saved_reference(delivery, surface, content):
    session, calls, _ = delivery
    session.api_url = "https://wrong-origin.test"
    result = invoke(surface, content)
    (saved,) = list_question_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert result["status"] == "unverified"
    assert result["error_code"] == "response_unverified"
    assert result["unresolved"] is True
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    assert saved.phase == "prepared"
    assert saved.result is None
    assert calls == []
    session.credentials.assert_not_called()
    writes.refresh_replica.assert_not_called()
