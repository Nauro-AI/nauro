"""Installed write entry points retain attempts across uncertain outcomes."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from nauro.auth import ActiveCredentials
from nauro.sync import generation_writes as writes
from nauro.sync.generation_credentials import GenerationConnection

PROJECT = "01K00000000000000000000001"
ACTOR = "01K00000000000000000000002"
CASES = [
    ("update_state", {"delta": "Frozen state"}),
    ("update_stack", {"content": "Frozen stack"}),
    ("flag_question", {"question": "Next step?"}),
    ("flag_question", {"resolved_by": "D42", "targets": ["Q1"]}),
]


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    binding = SimpleNamespace(project_id=PROJECT)
    session = Mock(binding=binding, actor=ACTOR, api_url="https://api.example.test")
    session.connection = GenerationConnection(
        endpoint="https://api.example.test/mcp",
        issuer="https://issuer.test/",
        client_id="client",
        audience="https://api.test",
        redirect_uri="http://127.0.0.1:8080/callback",
    )
    session.credentials.return_value = ActiveCredentials(ACTOR, "generation-token")
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(writes, "resolve_project_binding", lambda *a, **k: binding)
    monkeypatch.setattr(writes, "observe_generation_marker", lambda b: object())
    monkeypatch.setattr(writes, "GenerationTransferSession", lambda b: session)
    monkeypatch.setattr(writes, "refresh_replica", Mock())
    monkeypatch.setattr(
        writes, "capture_write_revision", Mock(return_value="a" * 64), raising=False
    )
    from nauro.sync import stack_writes

    monkeypatch.setattr(stack_writes, "capture_write_revision", writes.capture_write_revision)
    monkeypatch.setattr(writes, "replica_status", lambda b: {"installed_for_user_id": ACTOR})
    from nauro.cli import generation_writes as cli_writes
    from nauro.mcp import stdio_server

    regenerate = Mock(return_value={"status": "updated"})
    monkeypatch.setattr(cli_writes, "regenerate_refreshed_guidance", regenerate)
    monkeypatch.setattr(stdio_server, "regenerate_refreshed_guidance", regenerate)
    calls = []
    behavior = {"status": "committed", "drop": False}

    def handle(request):
        assert request.headers["Authorization"] == "Bearer generation-token"
        calls.append(request)
        if behavior["drop"]:
            raise httpx.ReadError("lost response")
        family = request.url.path.split("/")[1].removesuffix("s")
        records = importlib.import_module(f"nauro.store.{family}_records")
        (saved,) = getattr(records, f"list_{family}_submissions")(
            PROJECT, ACTOR, require_actor=session.require_actor
        )
        fixtures = importlib.import_module(f"tests.test_{family}_submission")
        return httpx.Response(200, json=fixtures._body(saved, behavior["status"]))

    session.client = httpx.Client(transport=httpx.MockTransport(handle))
    yield session, calls, behavior
    session.client.close()


@pytest.mark.parametrize("operation,content", CASES)
def test_public_stdio_commits_and_refreshes_same_authority(delivery, operation, content):
    from nauro.mcp import stdio_server

    session, calls, _ = delivery
    result = getattr(stdio_server, operation)(project_id=PROJECT, **content)
    assert result["status"] == "committed"
    assert result["guidance_status"] == {"status": "updated"}
    assert len(calls) == 1
    writes.refresh_replica.assert_called_once_with(
        session.binding, expected=(session.connection, ACTOR)
    )
    session.credentials.assert_called()


@pytest.mark.parametrize("operation,content", CASES)
def test_uncertain_attempt_discover_recover_and_retry(delivery, operation, content):
    _, calls, behavior = delivery
    behavior["drop"] = True
    uncertain = writes.generation_write(operation, content)
    assert uncertain["status"] == "unresolved"
    reference = {key: uncertain[key] for key in ("operation_id", "payload_digest")}
    found = writes.generation_write(operation, {"request_mode": "discover"})
    assert found["attempts"][0]["scope"]["operation_id"] == reference["operation_id"]
    behavior.update(drop=False, status="absent")
    recovered = writes.generation_write(operation, {"request_mode": "recover", **reference})
    assert recovered["status"] == "absent"
    assert [request.url.path.split("/")[-1] for request in calls] == ["submit", "lookup"]
    writes.generation_write(operation, {"request_mode": "retry", **reference})
    assert [request.url.path.split("/")[-1] for request in calls] == [
        "submit",
        "lookup",
        "lookup",
        "submit",
    ]
    bodies = [json.loads(request.content) for request in calls]
    assert bodies == [bodies[0]] * 4


def test_changed_connection_cannot_recover(delivery):
    session, calls, behavior = delivery
    behavior["drop"] = True
    result = writes.generation_write("update_state", {"delta": "Frozen state"})
    session.connection = session.connection.model_copy(update={"client_id": "other-client"})
    with pytest.raises(ValueError, match="connection"):
        writes.generation_write(
            "update_state",
            {
                "request_mode": "recover",
                "operation_id": result["operation_id"],
                "payload_digest": result["payload_digest"],
            },
        )
    assert len(calls) == 1


def test_committed_refresh_failure_never_resubmits(delivery):
    _, calls, _ = delivery
    writes.refresh_replica.side_effect = ValueError("offline")
    result = writes.generation_write("update_state", {"delta": "Frozen state"})
    assert result["status"] == "committed"
    assert result["replica_status"]["error_code"] == "receipt_refresh_required"
    recovered = writes.generation_write(
        "update_state",
        {
            "request_mode": "retry",
            "operation_id": result["operation_id"],
            "payload_digest": result["payload_digest"],
        },
    )
    assert recovered["status"] == "committed"
    assert len(calls) == 1


@pytest.mark.parametrize("operation,content", CASES)
def test_reference_modes_reject_content(delivery, operation, content):
    _, calls, _ = delivery
    with pytest.raises(ValueError, match="replace content"):
        writes.generation_write(operation, {"request_mode": "discover", **content})
    assert calls == []


def test_stale_revision_returns_original_reference(delivery):
    _, calls, behavior = delivery
    behavior["status"] = "revision_conflict_observed"
    result = writes.generation_write(
        "update_state", {"delta": "Frozen state", "expected_revision": "a" * 64}
    )
    assert result["status"] == "revision_conflict_observed"
    assert result["current_revision"] == "b" * 64
    assert result["operation_id"] == json.loads(calls[0].content)["operation_id"]
    assert result["guidance"] == (
        "Refresh the replica and re-read the current document before preparing a new write. "
        "This saved attempt retains its original revision."
    )


@pytest.mark.parametrize("command", ["update-state", "flag-question", "update-stack"])
def test_cli_discovery_uses_generation_entry_point(delivery, command):
    from nauro.cli.main import app

    result = CliRunner().invoke(app, [command, "--request-mode", "discover"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"status": "discovered", "attempts": []}


@pytest.mark.parametrize(
    "command",
    [
        ["update-state", "Frozen state"],
        ["update-stack", "Frozen stack"],
        ["flag-question", "Next step?"],
        ["flag-question", "--question", "Next step?"],
        ["flag-question", "--resolved-by", "D42", "--targets", "Q1"],
    ],
)
def test_cli_submits_each_public_operation(delivery, command):
    from nauro.cli.main import app

    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "committed"
    assert json.loads(result.output)["guidance_status"] == {"status": "updated"}


@pytest.mark.parametrize("command", ["update-state", "flag-question", "update-stack"])
@pytest.mark.parametrize(
    "status,exit_code", [("committed", 0), ("unresolved", 1), ("discovered", 0)]
)
def test_cli_text_preserves_status_and_saved_reference(monkeypatch, command, status, exit_code):
    from nauro.cli import generation_writes as cli_writes
    from nauro.cli.main import app

    response = {
        "status": status,
        "operation_id": "saved-operation",
        "payload_digest": "a" * 64,
        "unresolved": status == "unresolved",
        "guidance": "Keep this reference.",
        "replica_status": {"error_code": "receipt_refresh_required"},
    }
    monkeypatch.setattr(cli_writes, "generation_write", lambda *a, **k: response)
    result = CliRunner().invoke(app, [command, "--request-mode", "discover", "--format", "text"])
    assert result.exit_code == exit_code
    unresolved = "true" if status == "unresolved" else "false"
    assert result.stdout == (
        f"status: {status}\noperation_id: saved-operation\npayload_digest: {'a' * 64}\n"
        f"unresolved: {unresolved}\nguidance: Keep this reference.\n"
        'replica_status: {"error_code": "receipt_refresh_required"}\n'
    )
    json_result = CliRunner().invoke(app, [command, "--request-mode", "discover", "--no-json"])
    assert json_result.exit_code == exit_code
    assert json.loads(json_result.stdout) == response


def test_account_switch_refuses_before_send(delivery):
    session, calls, _ = delivery
    session.require_actor.side_effect = ValueError("account changed")
    with pytest.raises(ValueError, match="account changed"):
        writes.generation_write("update_state", {"delta": "Frozen state"})
    assert calls == []


@pytest.mark.parametrize("operation,content", CASES)
@pytest.mark.parametrize(
    "failure,status,code",
    [
        ("RecoveryRequiredError", "recovery_required", "lookup_required"),
        ("TransportError", "unverified", "response_unverified"),
        ("GenerationConnectionError", "blocked", "submission_authority_unavailable"),
        ("SubmissionActorMismatchError", "blocked", "submission_authority_unavailable"),
        ("SubmissionRecordCorruptError", "blocked", "submission_record_invalid"),
        ("SubmissionRecordError", "blocked", "submission_record_unavailable"),
        ("OSError", "blocked", "submission_record_unavailable"),
        ("ValueError", "unverified", "write_outcome_unverified"),
    ],
)
def test_write_failure_preserves_reference_and_action(
    delivery, monkeypatch, operation, content, failure, status, code
):
    from nauro.store import submission_records
    from nauro.sync.generation_session import GenerationConnectionError

    session, calls, _ = delivery
    family = writes.FAMILIES[operation]
    submission = importlib.import_module(f"nauro.sync.{family}_submission")
    contract = importlib.import_module(f"nauro.store.{family}_contract")
    errors = {
        "RecoveryRequiredError": getattr(submission, family.title() + "RecoveryRequiredError"),
        "TransportError": getattr(contract, family.title() + "TransportError"),
        "GenerationConnectionError": GenerationConnectionError,
        "SubmissionActorMismatchError": submission_records.SubmissionActorMismatchError,
        "SubmissionRecordCorruptError": submission_records.SubmissionRecordCorruptError,
        "SubmissionRecordError": submission_records.SubmissionRecordError,
        "OSError": OSError,
        "ValueError": ValueError,
    }
    monkeypatch.setattr(
        submission, f"submit_{family}", Mock(side_effect=errors[failure]("PRIVATE"))
    )
    result = writes.generation_write(operation, content)
    records = importlib.import_module(f"nauro.store.{family}_records")
    (saved,) = getattr(records, f"list_{family}_submissions")(
        PROJECT, ACTOR, require_actor=session.require_actor
    )
    assert result["status"] == status
    assert result["error_code"] == code
    assert result["unresolved"] is True
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    assert "PRIVATE" not in json.dumps(result)
    assert calls == []


@pytest.mark.parametrize("operation,content", CASES)
@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_expired_retry_only_looks_up_original_attempt(delivery, operation, content, surface):
    from nauro.cli.main import app
    from nauro.mcp import stdio_server

    session, calls, behavior = delivery
    behavior["drop"] = True
    result = writes.generation_write(operation, content)
    family = writes.FAMILIES[operation]
    records = importlib.import_module(f"nauro.store.{family}_records")
    (record,) = getattr(records, f"list_{family}_submissions")(
        PROJECT, ACTOR, require_actor=session.require_actor
    )
    records._write(record.model_copy(update={"created_at": "2020-01-01T00:00:00.000000Z"}))
    behavior.update(drop=False, status="absent")
    reference = {key: result[key] for key in ("operation_id", "payload_digest")}
    if surface == "cli":
        response = CliRunner().invoke(
            app,
            [
                operation.replace("_", "-"),
                "--request-mode",
                "retry",
                "--operation-id",
                reference["operation_id"],
                "--payload-digest",
                reference["payload_digest"],
            ],
        )
        assert response.exit_code == 1, response.output
        retried = json.loads(response.stdout)
    else:
        response = getattr(stdio_server, operation)(request_mode="retry", **reference)
        assert response.isError is True
        retried = json.loads(response.content[0].text)
    assert retried == {
        **reference,
        "status": "retry_expired",
        "error_code": "retry_horizon_expired",
        "unresolved": True,
        "guidance": (
            "The original 24-hour retry window has expired. Do not resend this attempt. "
            "Reconcile its outcome before creating a new write."
        ),
    }
    route = "questions" if family == "question" else family
    assert [request.url.path for request in calls] == [f"/{route}/submit", f"/{route}/lookup"]
    assert [json.loads(request.content)["operation_id"] for request in calls] == [
        reference["operation_id"],
        reference["operation_id"],
    ]


@pytest.mark.parametrize("operation,content", CASES)
def test_saved_generation_attempt_survives_process_restart(delivery, operation, content):
    import os
    import subprocess
    import sys

    session, _, behavior = delivery
    behavior["drop"] = True
    result = writes.generation_write(operation, content)
    script = """
import importlib, json, sys
records = importlib.import_module("nauro.store." + sys.argv[3] + "_records")
record, = getattr(records, "list_" + sys.argv[3] + "_submissions")(
    sys.argv[1], sys.argv[2], require_actor=lambda actor: None
)
print(json.dumps({
    'connection': record.connection,
    'operation_id': record.scope.operation_id,
    'phase': record.phase
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, PROJECT, ACTOR, writes.FAMILIES[operation]],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "connection": session.connection.binding(),
        "operation_id": result["operation_id"],
        "phase": "uncertain",
    }


def test_refresh_account_switch_keeps_committed_receipt(delivery, monkeypatch):
    monkeypatch.setattr(
        writes, "replica_status", lambda b: {"installed_for_user_id": "another-actor"}
    )
    result = writes.generation_write("update_state", {"delta": "Frozen state"})
    assert result["status"] == "committed"
    assert result["replica_status"]["error_code"] == "receipt_refresh_required"


def test_successful_refresh_regenerates_guidance(delivery):
    regenerate = Mock(return_value={"status": "updated"})
    result = writes.generation_write(
        "update_state", {"delta": "Frozen state"}, on_refreshed=regenerate
    )
    assert result["guidance_status"] == {"status": "updated"}
    regenerate.assert_called_once_with(writes.refresh_replica.return_value)


def test_registered_state_persists_literal_null(delivery):
    import asyncio

    from nauro.mcp.stdio_server import mcp
    from nauro.store.state_records import list_state_submissions

    session, calls, _ = delivery
    result = asyncio.run(mcp._tool_manager.get_tool("update_state").run({"delta": "null"}))
    assert result["status"] == "committed"
    (saved,) = list_state_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert json.loads(saved.payload_json)["delta"] == "null"
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["noop_observed", "revision_conflict_observed"])
@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_nonterminal_observations_preserve_saved_attempt(delivery, status, surface):
    import asyncio

    from nauro.cli.main import app
    from nauro.mcp.stdio_server import mcp
    from nauro.store.state_records import list_state_submissions

    session, calls, behavior = delivery
    behavior["status"] = status
    if surface == "cli":
        result = CliRunner().invoke(app, ["update-state", "Frozen state"])
        assert result.exit_code == 1
        value = json.loads(result.stdout)
    else:
        result = asyncio.run(
            mcp._tool_manager.get_tool("update_state").run({"delta": "Frozen state"})
        )
        assert result.isError is True
        value = json.loads(result.content[0].text)
    assert value["status"] == status
    assert value["unresolved"] is True
    (saved,) = list_state_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert saved.scope.operation_id == value["operation_id"]
    assert saved.payload_digest == value["payload_digest"]
    assert saved.payload_json == json.loads(calls[0].content)["payload_json"]
    writes.refresh_replica.assert_not_called()


@pytest.mark.parametrize("selected", [PROJECT, "Target", "", "missing"])
@pytest.mark.parametrize(
    "operation,content", [case for case in CASES if "resolved_by" not in case[1]]
)
def test_cli_explicit_project_from_another_repo(
    delivery, tmp_path, monkeypatch, selected, operation, content
):
    from nauro.cli.main import app
    from nauro.store.registry import register_project_v2
    from nauro.store.repo_config import save_repo_config
    from nauro.store.resolution import resolve_project_binding

    session, calls, _ = delivery
    other = tmp_path / "other"
    other.mkdir()
    other_id, _ = register_project_v2("Other", [other])
    save_repo_config(other, {"mode": "local", "id": other_id, "name": "Other"})
    register_project_v2("Target", [], project_id=PROJECT)
    monkeypatch.chdir(other)
    monkeypatch.setattr(writes, "resolve_project_binding", resolve_project_binding)
    session.binding = resolve_project_binding(PROJECT, None, use_cwd=False)
    create_session = Mock(return_value=session)
    monkeypatch.setattr(writes, "GenerationTransferSession", create_session)

    result = CliRunner().invoke(
        app, [operation.replace("_", "-"), next(iter(content.values())), "--project", selected]
    )

    if selected in {"", "missing"}:
        assert result.exit_code == 1, result.output
        assert f"Unknown project '{selected}'." in result.output
        assert "Available projects: Other, Target" in result.output
        create_session.assert_not_called()
        assert calls == []
        return
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "committed"
    create_session.assert_called_once_with(session.binding)
    assert json.loads(calls[0].content)["project_id"] == PROJECT

    import asyncio

    from nauro.mcp.stdio_server import mcp

    calls.clear()
    refused = asyncio.run(
        mcp._tool_manager.get_tool(operation).run(
            {**content, "project_id": PROJECT, "cwd": str(other)}
        )
    )
    assert "does not match" in str(refused)
    assert calls == []


@pytest.mark.parametrize("surface", ["cli", "stdio"])
def test_guidance_exception_preserves_committed_receipt(delivery, monkeypatch, surface):
    import asyncio

    from nauro.cli import generation_writes as cli_writes
    from nauro.cli.main import app
    from nauro.mcp import stdio_server
    from nauro.store import state_records

    session, calls, _ = delivery
    callback = Mock(side_effect=RuntimeError("PRIVATE FAILURE"))
    monkeypatch.setattr(cli_writes, "regenerate_refreshed_guidance", callback)
    monkeypatch.setattr(stdio_server, "regenerate_refreshed_guidance", callback)
    if surface == "cli":
        response = CliRunner().invoke(app, ["update-state", "Frozen state"])
        assert response.exit_code == 0, response.output
        result = json.loads(response.stdout)
    else:
        result = asyncio.run(
            stdio_server.mcp._tool_manager.get_tool("update_state").run({"delta": "Frozen state"})
        )
    (saved,) = state_records.list_state_submissions(
        PROJECT, ACTOR, require_actor=session.require_actor
    )
    assert result["status"] == "committed"
    assert result["receipt_json"] == saved.result.receipt_json
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    assert result["guidance_status"] == {
        "status": "failed",
        "message": "Replica refresh completed, but guidance regeneration failed. "
        "Run 'nauro sync' to regenerate guidance. Do not resubmit the write.",
    }
    assert "PRIVATE" not in json.dumps(result)
    assert len(calls) == 1


def test_guidance_does_not_swallow_interrupt(delivery):
    with pytest.raises(KeyboardInterrupt):
        writes.generation_write(
            "update_state",
            {"delta": "Frozen state"},
            on_refreshed=Mock(side_effect=KeyboardInterrupt),
        )


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize("mode", ["discover", "recover", "retry", "submit"])
def test_saved_record_failure_is_structured(delivery, monkeypatch, mode, surface):
    import asyncio

    from nauro.cli.main import app
    from nauro.mcp.stdio_server import mcp
    from nauro.store import state_records
    from nauro.store.state_contract import state_payload

    session, calls, _ = delivery
    saved = state_records.prepare_state_submission(
        PROJECT,
        ACTOR,
        state_payload("Frozen state"),
        connection=session.connection.binding(),
        require_actor=session.require_actor,
    )
    path = state_records._record_path(saved.scope)
    path.write_text("PRIVATE CORRUPT RECORD")
    reference = {"operation_id": saved.scope.operation_id, "payload_digest": saved.payload_digest}
    arguments = {"request_mode": mode}
    if mode in {"recover", "retry"}:
        arguments.update(reference)
    elif mode == "submit":
        arguments["delta"] = "New state"
        monkeypatch.setattr(
            state_records.uuid, "uuid4", lambda: SimpleNamespace(hex=saved.scope.operation_id)
        )
    if surface == "cli":
        argv = ["update-state"]
        for key, value in arguments.items():
            if key == "delta":
                argv.append(value)
            else:
                argv.extend(["--" + key.replace("_", "-"), value])
        response = CliRunner().invoke(app, argv)
        assert response.exit_code == 1, response.output
        result = json.loads(response.stdout)
    else:
        response = asyncio.run(mcp._tool_manager.get_tool("update_state").run(arguments))
        assert response.isError is True
        result = json.loads(response.content[0].text)
    assert result == {
        **(reference if mode in {"recover", "retry"} else {}),
        "status": "blocked",
        "error_code": "submission_record_invalid",
        "unresolved": True,
        "guidance": "Preserve the saved record and repair its local storage. "
        "Reconcile the original operation before creating a new write.",
    }
    assert path.read_text() == "PRIVATE CORRUPT RECORD"
    assert calls == []


def test_unavailable_prepare_lock_returns_structured_failure(delivery, monkeypatch):
    from filelock import Timeout

    from nauro.cli.main import app
    from nauro.store import state_records

    _, calls, _ = delivery
    monkeypatch.setattr(
        state_records, "state_submission_lock", Mock(side_effect=Timeout("PRIVATE LOCK"))
    )
    response = CliRunner().invoke(app, ["update-state", "Frozen state"])
    assert response.exit_code == 1
    assert json.loads(response.stdout) == {
        "status": "blocked",
        "error_code": "submission_record_unavailable",
        "unresolved": True,
        "guidance": "Restore access to the saved record, "
        "then recover this reference before another write.",
    }
    assert calls == []


@pytest.mark.parametrize(
    "command,filename",
    [("update-state", "state_current.md"), ("flag-question", "open-questions.md")],
)
def test_cli_legacy_target_survives_unrelated_strict_registry_error(
    tmp_path, monkeypatch, command, filename
):
    from nauro.cli.main import app
    from nauro.demo import create_demo_project
    from nauro.store.registry import load_registry_v2, register_project_v2, save_registry_v2
    from nauro.store.resolution import StoreResolutionError, resolve_project_binding

    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    _, store = register_project_v2("Target", [])
    create_demo_project(store)
    other, _ = register_project_v2("Other", [])
    registry = load_registry_v2()
    registry["projects"][other]["repo_paths"] = ["relative/path"]
    save_registry_v2(registry)
    with pytest.raises(StoreResolutionError):
        resolve_project_binding("Target", None, use_cwd=False)

    response = CliRunner().invoke(app, [command, "New legacy state", "--project", "Target"])

    assert response.exit_code == 0, response.output
    assert "New legacy state" in (store / filename).read_text()


@pytest.mark.parametrize("surface", ["cli", "stdio"])
@pytest.mark.parametrize(
    "status,code", [(403, "project_not_selected"), (409, "single_writer_refused")]
)
def test_public_state_refusal_keeps_saved_reference(delivery, surface, status, code):
    import asyncio

    from nauro.cli.main import app
    from nauro.mcp.stdio_server import mcp
    from nauro.store.state_records import list_state_submissions

    session, _, _ = delivery
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": code}))
    ) as client:
        session.client = client
        if surface == "cli":
            response = CliRunner().invoke(app, ["update-state", "Frozen state"])
            assert response.exit_code == 1, response.output
            result = json.loads(response.stdout)
        else:
            response = asyncio.run(
                mcp._tool_manager.get_tool("update_state").run({"delta": "Frozen state"})
            )
            assert response.isError is True
            result = json.loads(response.content[0].text)
    (saved,) = list_state_submissions(PROJECT, ACTOR, require_actor=session.require_actor)
    assert result["status"] == "refused"
    assert result["unresolved"] is False
    assert result["error_code"] == code
    assert result["server_code"] == code
    assert result["http_status"] == status
    assert result["operation_id"] == saved.scope.operation_id
    assert result["payload_digest"] == saved.payload_digest
    assert saved.phase == "resolved"
    assert "This attempt did not write." in result["guidance"]
    writes.refresh_replica.assert_not_called()
