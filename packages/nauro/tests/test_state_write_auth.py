"""Public state writes keep recoverable evidence when generation authority changes."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.stdio_server import mcp
from nauro.store import state_records
from nauro.sync.generation_session import GenerationConnectionError
from tests.test_generation_write_delivery import ACTOR, PROJECT, delivery

__all__ = ["delivery"]


def invoke(surface, arguments):
    if surface == "stdio":
        result = asyncio.run(mcp._tool_manager.get_tool("update_state").run(arguments))
        return json.loads(result.content[0].text) if hasattr(result, "isError") else result
    command = ["update-state"]
    for key, value in arguments.items():
        command.extend([value] if key == "delta" else ["--" + key.replace("_", "-"), value])
    result = CliRunner().invoke(app, command)
    payload = json.loads(result.stdout)
    assert result.exit_code == (0 if payload["status"] in {"committed", "discovered"} else 1)
    return payload


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("boundary", ["record", "credentials", "refresh", "constructor"])
def test_state_reference_survives_authority_failures(delivery, surface, boundary):
    session, calls, behavior = delivery
    arguments = {"delta": "Frozen"}
    if boundary == "record":
        behavior["drop"] = True
        first = invoke(surface, arguments)
        arguments = {
            "request_mode": "recover",
            **{k: first[k] for k in ("operation_id", "payload_digest")},
        }
        session.require_actor.side_effect = GenerationConnectionError("Account changed")
    elif boundary == "credentials":
        session.credentials.side_effect = GenerationConnectionError("Account changed")
    elif boundary == "refresh":
        session.require_binding.side_effect = GenerationConnectionError("Account changed")
    else:
        session.api_url = "https://wrong-origin.test"
    result = invoke(surface, arguments)
    session.require_actor.side_effect = None
    (saved,) = state_records.list_state_submissions(
        PROJECT, ACTOR, require_actor=session.require_actor
    )
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    if boundary == "refresh":
        assert result["status"] == "committed"
        assert result["receipt_json"] == saved.result.receipt_json
        assert result["replica_status"]["error_code"] == "receipt_refresh_required"
    else:
        assert result["error_code"] == (
            "response_unverified"
            if boundary == "constructor"
            else "submission_authority_unavailable"
        )
        assert result["unresolved"] is True
    assert len(calls) == int(boundary in {"record", "refresh"})
    if boundary == "constructor":
        session.credentials.assert_not_called()


@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_interrupted_local_state_prepare_directs_discovery(delivery, monkeypatch, surface):
    session, calls, _ = delivery
    write = state_records._write

    def interrupted(record):
        write(record)
        session.require_actor.side_effect = GenerationConnectionError("Account changed")

    monkeypatch.setattr(state_records, "_write", interrupted)
    result = invoke(surface, {"delta": "Frozen"})
    assert result["unresolved"] is False
    assert "operation_id" not in result
    assert "No request was sent" in result["guidance"]
    assert "discover" in result["guidance"]
    assert calls == []
    session.require_actor.side_effect = None
    (saved,) = invoke(surface, {"request_mode": "discover"})["attempts"]
    assert saved["phase"] == "prepared"
    assert saved["result"] is None
