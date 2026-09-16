import json
import time
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from nauro.cli import judgment_repair as repair
from nauro.cli.main import app
from nauro.store.recovery_actions import RecoveryActionStore, timestamp
from nauro.sync.generation_credentials import AccountRecord, GenerationConnection
from tests.test_recovery_actions import ACTOR, CREATED, DEADLINE, PROJECT, SAGA
from tests.test_recovery_transport import lookup, response

runner = CliRunner()


def observation():
    return {
        "kind": "inspection",
        "lane": None,
        "saga": {
            "project_id": PROJECT,
            "saga_id": SAGA,
            "status": "recovery_required",
            "idempotency_user_id": ACTOR,
            "idempotency_operation_id": "original",
            "payload_digest": "b" * 64,
            "fencing_token": 2,
            "lease_owner": None,
            "lease_expires_at": None,
            "plan_record_bytes": "not displayed",
        },
        "saved_request": None,
        "execution_deadline": DEADLINE,
        "current_state": None,
    }


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    connection = GenerationConnection(
        endpoint="https://probe.example/mcp",
        issuer="https://auth.example/",
        client_id="synthetic",
        audience="https://probe.example/mcp",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    account = connection.store()
    with account.locked():
        account.write(
            AccountRecord(
                revision="revision",
                binding=connection.binding(),
                state="active",
                user_id=ACTOR,
                subject="synthetic",
                access_token="synthetic",
                refresh_token="synthetic",
                expires_at=int(time.time()) + 3600,
            )
        )
    monkeypatch.setattr(repair, "attachment_connection", lambda _: connection)
    monkeypatch.setattr(repair, "_now", lambda: timestamp(CREATED))
    store = RecoveryActionStore(connection.binding(), PROJECT, ACTOR)
    state = SimpleNamespace(
        calls=[], accepted=False, lose=False, connection=connection, store=store
    )

    def handle(request):
        body = json.loads(request.content)
        state.calls.append(body)
        mode = body["mode"]
        if mode == "inspect":
            result = observation()
        elif mode == "discover":
            result = {"kind": "page", "actions": [], "next_after": None}
        else:
            saved = store.list()
            record = saved[0] if saved else None
            result = lookup(record, "accepted" if state.accepted else "absent")
            result["scope"]["operation_id"] = body["action_id"]
            if mode == "dispatch":
                assert record is not None
                assert record.action_payload == body["action_payload"]
                state.accepted = True
                if state.lose:
                    raise httpx.ReadTimeout("lost response")
                result = {
                    "kind": "resume_accepted",
                    "receipt_json": lookup(record, "accepted")["receipt_json"],
                    "source": "executed",
                    "current_state": {"kind": "active"},
                    "dispatch_blocked": None,
                }
        return response(request, result)

    client_type = httpx.Client
    monkeypatch.setattr(
        repair.httpx, "Client", lambda **_: client_type(transport=httpx.MockTransport(handle))
    )
    return state


def invoke(*args, input=""):
    return runner.invoke(app, ["repair", "--judgment", "--project", PROJECT, *args], input=input)


def test_inspection_is_read_only_without_replica(hosted):
    result = invoke()
    assert result.exit_code == 0, result.output
    assert [call["mode"] for call in hosted.calls] == ["inspect", "discover"]
    assert hosted.store.list() == ()
    assert "not displayed" not in result.output


def test_declined_action_is_not_saved_or_sent(hosted):
    result = invoke("--resume", input="n\n")
    assert result.exit_code == 0, result.output
    assert [call["mode"] for call in hosted.calls] == ["inspect", "lookup"]
    assert hosted.store.list() == ()


def test_lost_response_restart_lookup_and_resend_do_not_execute_again(hosted):
    hosted.lose = True
    result = invoke("--resume", input="y\n")
    assert result.exit_code == 1, result.output
    (saved,) = hosted.store.list()
    assert saved.action_id in result.output
    assert [call["mode"] for call in hosted.calls] == ["inspect", "lookup", "dispatch"]
    for option in ("--action", "--resend-action"):
        result = invoke(option, saved.action_id)
        assert result.exit_code == (0 if option == "--action" else 1), result.output
        assert "Server action" in result.output
    assert [call["mode"] for call in hosted.calls][-2:] == ["lookup", "lookup"]
    assert hosted.store.list() == (saved,)


def test_unaccepted_resend_preserves_identity_and_bytes(hosted):
    saved = repair._prepare(observation(), hosted.store, "resume")
    hosted.store.save(saved)
    result = invoke("--resend-action", saved.action_id, input="y\n")
    assert result.exit_code == 1, result.output  # Accepted but execution remains active.
    assert [call["mode"] for call in hosted.calls] == ["lookup", "dispatch"]
    assert hosted.calls[-1]["action_id"] == saved.action_id
    assert hosted.calls[-1]["action_payload"] == saved.action_payload
    assert hosted.store.list() == (saved,)


def test_missing_resend_never_constructs_new_action(hosted):
    result = invoke("--resend-action", "missing")
    assert result.exit_code == 1
    assert hosted.calls == []
    assert hosted.store.list() == ()


def test_deadline_crossing_during_confirmation_prevents_send(hosted, monkeypatch):
    def confirm(*args, **kwargs):
        monkeypatch.setattr(repair, "_now", lambda: timestamp(DEADLINE))
        return True

    monkeypatch.setattr(repair.typer, "confirm", confirm)
    result = invoke("--resume")
    assert result.exit_code == 1
    assert [call["mode"] for call in hosted.calls] == ["inspect", "lookup"]
    assert hosted.store.list() == ()


def test_credential_change_during_confirmation_prevents_send(hosted, monkeypatch):
    def confirm(*args, **kwargs):
        store = hosted.connection.store()
        with store.locked():
            record = store.read()
            store.write(record.model_copy(update={"state": "logged_out"}))
        return True

    monkeypatch.setattr(repair.typer, "confirm", confirm)
    result = invoke("--resume")
    assert result.exit_code == 1
    assert [call["mode"] for call in hosted.calls] == ["inspect", "lookup"]


@pytest.mark.parametrize(
    "options",
    [
        ["--resume", "--abandon"],
        ["--action", "id", "--resume"],
        ["--resend-action", "id", "--saga", SAGA],
        ["--resend-action", "id", "--action", "id"],
    ],
)
def test_invalid_mode_combinations_do_not_connect(hosted, options):
    result = invoke(*options)
    assert result.exit_code == 2
    assert hosted.calls == []


def test_repeated_cursor_stops_before_second_prompt(monkeypatch):
    prompts = []
    monkeypatch.setattr(repair.typer, "confirm", lambda *a, **kw: prompts.append(1) or True)
    client = SimpleNamespace(discover=lambda _: {"next_after": "same"})
    with pytest.raises(ValueError, match="advance"):
        repair._discover(client)
    assert len(prompts) == 1


def test_discovery_has_hard_page_limit(monkeypatch, capsys):
    calls = []

    def discover(_):
        calls.append(1)
        return {"next_after": f"action:{len(calls):05d}"}

    monkeypatch.setattr(repair.typer, "confirm", lambda *a, **kw: True)
    repair._discover(SimpleNamespace(discover=discover))
    assert len(calls) == 100
    assert "not proof of absence" in capsys.readouterr().out


@pytest.mark.parametrize("shape", ["directory", "broken_symlink"])
def test_local_repair_refuses_replica_before_planning(tmp_path, monkeypatch, shape):
    from nauro.cli.commands import repair as local

    path = tmp_path / ".replica"
    if shape == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "missing")
    monkeypatch.setattr(local, "resolve_target_project", lambda _: ("project", tmp_path))
    monkeypatch.setattr(local, "plan_supersede_repair", lambda *_: pytest.fail("legacy planning"))
    result = runner.invoke(app, ["repair"])
    assert result.exit_code == 1
    assert "refuses generation" in result.output
