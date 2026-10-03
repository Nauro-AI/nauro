"""Sharing surfaces preserve immutable attempts and reject invalid mode content."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.stdio_server import mcp
from nauro.store.share_records import list_share_submissions
from nauro.sync import generation_writes as writes
from nauro.sync.generation_session import GenerationConnectionError
from tests.test_generation_write_delivery import ACTOR, PROJECT, delivery

__all__ = ["delivery"]
CONTENT = {"slug": "null", "content": "null", "pointer_kind": "brief", "summary": "null"}


def call(surface, mode="submit", **arguments):
    if surface == "stdio":
        result = asyncio.run(
            mcp._tool_manager.get_tool("share_context").run({"request_mode": mode, **arguments})
        )
        if hasattr(result, "content"):
            payload = json.loads(result.content[0].text)
            assert result.isError is (payload["status"] not in {"committed", "discovered"})
            return payload
        return result
    command = ["share-context", "--request-mode", mode]
    command += [arguments.pop(name) for name in ("slug", "content") if name in arguments]
    for key, value in arguments.items():
        command.extend(["--" + key.replace("_", "-"), value])
    result = CliRunner().invoke(app, command)
    payload = json.loads(result.stdout)
    assert result.exit_code == (0 if payload["status"] in {"committed", "discovered"} else 1)
    return payload


@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_public_modes_keep_literal_strings_and_reference(delivery, surface):
    session, calls, behavior = delivery
    behavior["drop"] = True
    first = call(surface, **CONTENT)
    reference = {key: first[key] for key in ("operation_id", "payload_digest")}
    assert (
        call(surface, "discover")["attempts"][0]["scope"]["operation_id"]
        == reference["operation_id"]
    )
    behavior.update(drop=False, status="absent")
    assert call(surface, "recover", **reference)["status"] == "absent"
    behavior["submit_status"] = "committed"
    assert call(surface, "retry", **reference)["status"] == "committed"
    assert [request.url.path for request in calls] == [
        "/share/submit",
        "/share/lookup",
        "/share/lookup",
        "/share/submit",
    ]
    bodies = [json.loads(request.content) for request in calls]
    assert bodies == [bodies[0]] * 4
    assert json.loads(bodies[0]["payload_json"]) == {"operation": "share_context", **CONTENT}
    (saved,) = list_share_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert saved.connection == session.connection.binding()
    writes.refresh_replica.assert_called_once()
    session.connection = session.connection.model_copy(
        update={"endpoint": "https://other.test/mcp"}
    )
    assert call(surface, "discover") == {"status": "discovered", "attempts": []}
    assert len(calls) == 4


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"request_mode": "submit"},
        {**CONTENT, "request_mode": "null"},
        {**CONTENT, "operation_id": "caller"},
        {**CONTENT, "expected_revision": "a" * 64},
        {"request_mode": "discover", "payload_digest": "a" * 64},
        {
            "request_mode": "recover",
            "operation_id": "saved",
            "payload_digest": "a" * 64,
            "content": "null",
        },
        *[{key: value for key, value in CONTENT.items() if key != missing} for missing in CONTENT],
        *[
            {**CONTENT, key: value}
            for key, value in [
                ("slug", "../brief"),
                ("pointer_kind", "null"),
                ("summary", ""),
                ("content", "x" * 51201),
            ]
        ],
    ],
)
@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_registered_schema_and_runtime_refuse_invalid_content(delivery, arguments, surface):
    import jsonschema

    _, calls, _ = delivery
    tool = mcp._tool_manager.get_tool("share_context")
    if not arguments or arguments == {"request_mode": "submit"}:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(arguments, tool.parameters)
    if surface == "stdio":
        with pytest.raises(ToolError):
            asyncio.run(tool.run(arguments))
    else:
        command = ["share-context"] + [
            arguments[key] for key in ("slug", "content") if key in arguments
        ]
        for key, value in arguments.items():
            if key not in {"slug", "content"}:
                command += ["--" + key.replace("_", "-"), value]
        assert CliRunner().invoke(app, command).exit_code == 2
    assert calls == []


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("boundary", ["record", "credentials", "refresh", "origin"])
def test_authority_failures_preserve_share_reference(delivery, surface, boundary):
    session, calls, behavior = delivery
    arguments, mode = CONTENT, "submit"
    if boundary == "record":
        behavior["drop"] = True
        first = call(surface, **CONTENT)
        arguments = {key: first[key] for key in ("operation_id", "payload_digest")}
        mode = "recover"
        session.require_actor.side_effect = GenerationConnectionError("Account changed")
    elif boundary == "credentials":
        session.credentials.side_effect = GenerationConnectionError("Account changed")
    elif boundary == "refresh":
        session.require_binding.side_effect = GenerationConnectionError("Account changed")
    else:
        session.connection = session.connection.model_copy(
            update={"endpoint": "https://other.test/mcp"}
        )
    result = call(surface, mode, **arguments)
    session.require_actor.side_effect = None
    (saved,) = list_share_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    if boundary == "refresh":
        assert result["status"] == "committed"
        assert result["replica_status"]["error_code"] == "receipt_refresh_required"
    else:
        assert result["unresolved"] is True
    assert len(calls) == (1 if boundary in {"record", "refresh"} else 0)


@pytest.mark.parametrize("failure", ["refresh", "guidance"])
def test_receipt_survives_local_failure(delivery, failure):
    _, calls, _ = delivery
    if failure == "refresh":
        writes.refresh_replica.side_effect = ValueError("offline")
    result = writes.generation_write(
        "share_context", CONTENT, on_refreshed=Mock(side_effect=RuntimeError("offline"))
    )
    assert result["status"] == "committed" and result["receipt_json"]
    assert (
        result["replica_status"]["error_code"] == "receipt_refresh_required"
        if failure == "refresh"
        else result["guidance_status"]["status"] == "failed"
    )
    assert len(calls) == 1


def test_conflict_keeps_uncertainty_and_original_reference(delivery):
    _, calls, behavior = delivery
    behavior["status"] = "slug_conflict_observed"
    result = call("stdio", **CONTENT)
    assert result["unresolved"] is True and result["status"] == "slug_conflict_observed"
    assert "Recover" in result["guidance"] or "recover" in result["guidance"]
    assert "before another operation" in result["guidance"]
    assert len(calls) == 1
    writes.refresh_replica.assert_not_called()


@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_legacy_refusal_never_creates_brief(tmp_path, monkeypatch, surface):
    from nauro.store.registry import register_project_v2

    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    _, store = register_project_v2("Legacy", [], project_id=PROJECT)
    before = {p.relative_to(store): p.read_bytes() for p in store.rglob("*") if p.is_file()}
    if surface == "cli":
        result = CliRunner().invoke(
            app,
            [
                "share-context",
                "brief",
                "Body",
                "--pointer-kind",
                "brief",
                "--summary",
                "Summary",
                "--project",
                "Legacy",
            ],
        )
        assert result.exit_code == 1 and "requires a generation replica" in result.output
    else:
        assert call(surface, **CONTENT, project_id=PROJECT)["error_code"] == "generation_required"
    assert {p.relative_to(store): p.read_bytes() for p in store.rglob("*") if p.is_file()} == before


def test_cli_help_and_text_reference(delivery):
    help_result = CliRunner().invoke(app, ["share-context", "--help"])
    assert help_result.exit_code == 0 and "immutable brief" in help_result.stdout
    _, _, behavior = delivery
    behavior["drop"] = True
    result = CliRunner().invoke(
        app,
        [
            "share-context",
            "brief",
            "Body",
            "--pointer-kind",
            "brief",
            "--summary",
            "Summary",
            "--format",
            "text",
        ],
    )
    assert result.exit_code == 1
    assert "operation_id:" in result.stdout and "payload_digest:" in result.stdout


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("mode", ["submit", "discover", "recover", "retry"])
@pytest.mark.parametrize("boundary", ["construction", "entry"])
def test_session_admission_failure_preserves_only_supplied_reference(
    delivery, monkeypatch, surface, mode, boundary
):
    session, calls, _ = delivery
    reference = {"operation_id": "saved-operation", "payload_digest": "a" * 64}
    arguments = CONTENT if mode == "submit" else reference if mode in {"recover", "retry"} else {}
    failure = GenerationConnectionError("PRIVATE account unavailable")
    if boundary == "construction":
        monkeypatch.setattr(writes, "GenerationTransferSession", Mock(side_effect=failure))
    else:
        session.__enter__.side_effect = failure
    result = call(surface, mode, **arguments)
    expected_reference = reference if mode in {"recover", "retry"} else {}
    assert result["status"] == "blocked"
    assert result["error_code"] == "submission_authority_unavailable"
    assert result["unresolved"] is bool(expected_reference)
    assert {key: result[key] for key in reference if key in result} == expected_reference
    assert "PRIVATE" not in json.dumps(result)
    assert list_share_submissions(PROJECT, ACTOR, require_actor=session.require_actor) == ()
    assert calls == []
    writes.refresh_replica.assert_not_called()


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("kind", ["brief", "resume", "selection"])
def test_advertised_pointer_kind_choices_match_canonical_runtime(delivery, surface, kind):
    import jsonschema
    from nauro_core.constants import POINTER_PREFIX_BY_KIND
    from typer.main import get_command

    tool = mcp._tool_manager.get_tool("share_context")
    options = tool.parameters["properties"]["pointer_kind"]["anyOf"]
    assert next(option["enum"] for option in options if "enum" in option) == list(
        POINTER_PREFIX_BY_KIND
    )
    command = get_command(app).commands["share-context"]
    pointer = next(parameter for parameter in command.params if parameter.name == "pointer_kind")
    assert list(pointer.type.choices) == list(POINTER_PREFIX_BY_KIND)
    help_result = CliRunner().invoke(app, ["share-context", "--help"])
    assert help_result.exit_code == 0
    assert "brief|resume|selection" in help_result.stdout
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**CONTENT, "pointer_kind": "unknown"}, tool.parameters)
    result = call(surface, **{**CONTENT, "pointer_kind": kind})
    assert result["status"] == "committed"
    _, calls, _ = delivery
    assert json.loads(json.loads(calls[0].content)["payload_json"])["pointer_kind"] == kind
