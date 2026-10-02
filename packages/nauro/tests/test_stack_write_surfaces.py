"""Stack entry points retain revision and recovery identity across failures."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.stdio_server import mcp
from nauro.store.stack_records import list_stack_submissions
from nauro.sync import generation_writes as writes
from tests.test_generation_write_delivery import ACTOR, PROJECT, delivery

__all__ = ["delivery"]


def _call(surface, mode="submit", **arguments):
    if surface == "stdio":
        result = asyncio.run(
            mcp._tool_manager.get_tool("update_stack").run({"request_mode": mode, **arguments})
        )
        return json.loads(result.content[0].text) if hasattr(result, "content") else result
    command = ["update-stack", "--request-mode", mode]
    if "content" in arguments:
        command.append(arguments.pop("content"))
    for key, value in arguments.items():
        command.extend(["--" + key.replace("_", "-"), value])
    result = CliRunner().invoke(app, command)
    payload = json.loads(result.stdout)
    assert result.exit_code == (0 if payload["status"] in {"committed", "discovered"} else 1)
    return payload


@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_saved_stack_modes_preserve_payload_and_revision(delivery, surface):
    _, calls, behavior = delivery
    behavior["drop"] = True
    submitted = _call(surface, content="null")
    assert submitted["status"] == "unresolved"
    reference = {key: submitted[key] for key in ("operation_id", "payload_digest")}
    found = _call(surface, "discover")
    assert found["attempts"][0]["scope"]["operation_id"] == reference["operation_id"]
    writes.capture_write_revision.return_value = "b" * 64
    behavior.update(drop=False, status="absent")
    assert _call(surface, "recover", **reference)["status"] == "absent"
    behavior["status"] = "committed"
    assert _call(surface, "retry", **reference)["status"] == "committed"
    assert [request.url.path for request in calls] == [
        "/stack/submit",
        "/stack/lookup",
        "/stack/lookup",
    ]
    bodies = [json.loads(request.content) for request in calls]
    assert bodies == [bodies[0]] * 3
    assert json.loads(bodies[0]["payload_json"]) == {
        "operation": "update_stack",
        "content": "null",
        "expected_revision": "a" * 64,
    }
    writes.capture_write_revision.assert_called_once()


@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_stale_stack_refusal_retains_saved_reference(delivery, surface):
    session, calls, behavior = delivery
    behavior["status"] = "revision_conflict_observed"
    result = _call(surface, content="Changed")
    assert result["status"] == "revision_conflict_observed"
    assert result["current_revision"] == "b" * 64
    (saved,) = list_stack_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    assert json.loads(calls[0].content)["payload_json"] == saved.payload_json
    writes.refresh_replica.assert_not_called()


@pytest.mark.parametrize("failure", ["refresh", "guidance"])
def test_stack_receipt_survives_local_failure(delivery, failure):
    _, calls, _ = delivery
    callback = Mock(side_effect=RuntimeError("guidance unavailable"))
    if failure == "refresh":
        writes.refresh_replica.side_effect = ValueError("offline")
    result = writes.generation_write("update_stack", {"content": "Python"}, on_refreshed=callback)
    assert result["status"] == "committed"
    if failure == "refresh":
        assert result["replica_status"]["error_code"] == "receipt_refresh_required"
    else:
        assert result["guidance_status"]["status"] == "failed"
    assert len(calls) == 1


def test_stack_origin_change_never_sends_credentials(delivery):
    session, calls, _ = delivery
    session.connection = session.connection.model_copy(
        update={"endpoint": "https://different.example.test/mcp"}
    )
    result = writes.generation_write("update_stack", {"content": "Python"})
    assert result["status"] == "unverified"
    (saved,) = list_stack_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    session.credentials.assert_not_called()
    assert calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": "Python", "request_mode": "null"},
        {"content": "Python", "expected_revision": "null"},
        {
            "request_mode": "recover",
            "operation_id": "a" * 32,
            "payload_digest": "b" * 64,
            "operation": "update_stack",
            "content": "null",
        },
    ],
)
def test_sdk_rejects_literal_null_control_fields(delivery, arguments):
    _, calls, _ = delivery
    with pytest.raises(ToolError):
        asyncio.run(mcp._tool_manager.get_tool("update_stack").run(arguments))
    assert calls == []


@pytest.mark.parametrize("mode", ["submit", "discover", "recover", "retry"])
@pytest.mark.parametrize("selected", ["missing", "Legacy", "incomplete"])
def test_cli_unknown_and_legacy_refuse_without_writes(tmp_path, monkeypatch, mode, selected):
    from nauro.store.registry import register_project_v2

    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    _, store = register_project_v2("Legacy", [], project_id=PROJECT)
    if selected == "incomplete":
        (store / ".replica").mkdir()
    command = [
        "update-stack",
        "--project",
        "Legacy" if selected == "incomplete" else selected,
        "--request-mode",
        mode,
    ]
    if mode == "submit":
        command.append("Python")
    elif mode in {"recover", "retry"}:
        command += ["--operation-id", "a" * 32, "--payload-digest", "b" * 64]
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1, result.output
    assert (
        "Unknown project 'missing'."
        if selected == "missing"
        else "Generation replica controls are incomplete"
        if selected == "incomplete"
        else "update-stack requires a generation replica."
    ) in result.output
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
