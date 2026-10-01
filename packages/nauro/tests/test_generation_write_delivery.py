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

PROJECT = "01K00000000000000000000001"
ACTOR = "01K00000000000000000000002"
CASES = [
    ("update_state", {"delta": "Frozen state"}),
    ("flag_question", {"question": "Which option?"}),
    ("flag_question", {"targets": ["Q1"], "resolved_by": "D42"}),
    ("update_stack", {"content": "Python"}),
    (
        "share_context",
        {"slug": "brief", "content": "Details", "pointer_kind": "brief", "summary": "Summary"},
    ),
]


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    binding = SimpleNamespace(project_id=PROJECT)
    session = Mock(binding=binding, actor=ACTOR, api_url="https://api.example.test")
    session.connection.binding.return_value = "connection-a"
    session.credentials.return_value = ActiveCredentials(ACTOR, "generation-token")
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(writes, "resolve_project_binding", lambda *a, **k: binding)
    monkeypatch.setattr(writes, "observe_generation_marker", lambda b: object())
    monkeypatch.setattr(writes, "GenerationTransferSession", lambda b: session)
    monkeypatch.setattr(writes, "refresh_replica", Mock())
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
        family = request.url.path.split("/")[1]
        if family == "questions":
            family = "question"
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
    session.connection.binding.return_value = "connection-b"
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


@pytest.mark.parametrize(
    "command", ["update-state", "flag-question", "update-stack", "share-context"]
)
def test_cli_discovery_uses_generation_entry_point(delivery, command):
    from nauro.cli.main import app

    result = CliRunner().invoke(app, [command, "--request-mode", "discover"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"status": "discovered", "attempts": []}


@pytest.mark.parametrize(
    "command",
    [
        ["update-state", "Frozen state"],
        ["flag-question", "Which option?"],
        ["flag-question", "--targets", "Q1", "--resolved-by", "D42"],
        ["update-stack", "Python"],
        ["share-context", "brief", "Details", "brief", "Summary"],
    ],
)
def test_cli_submits_each_public_operation(delivery, command):
    from nauro.cli.main import app

    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "committed"
    assert json.loads(result.output)["guidance_status"] == {"status": "updated"}


@pytest.mark.parametrize(
    "command", ["update-state", "flag-question", "update-stack", "share-context"]
)
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


def test_saved_generation_attempt_survives_process_restart(delivery):
    import os
    import subprocess
    import sys

    _, _, behavior = delivery
    behavior["drop"] = True
    result = writes.generation_write("update_state", {"delta": "Frozen state"})
    script = """
import json, sys
from nauro.store.state_records import list_state_submissions
record, = list_state_submissions(sys.argv[1], sys.argv[2], require_actor=lambda actor: None)
print(json.dumps({
    'connection': record.connection,
    'operation_id': record.scope.operation_id,
    'phase': record.phase
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, PROJECT, ACTOR],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "connection": "connection-a",
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
