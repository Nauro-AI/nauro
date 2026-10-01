"""Stack attempts retain their connection and use the session's actor guard."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.store import stack_records as records
from nauro.store.stack_contract import (
    StackTransportError,
    stack_payload,
)
from nauro.store.submission_records import SubmissionActorMismatchError
from nauro.sync import stack_submission as submission
from nauro.sync.generation_credentials import GenerationConnection
from nauro.sync.stack_transport import HttpStackTransport
from tests.test_stack_submission import OTHER, PROJECT, USER, _body, _prepared, home

__all__ = ["home"]


def connection_for(origin="https://example.test", client_id="client"):
    return GenerationConnection(
        endpoint=origin + "/mcp",
        issuer="https://issuer.test/",
        client_id=client_id,
        audience="https://api.test",
        redirect_uri="http://127.0.0.1:8080/callback",
    )


def test_session_authority_survives_reload_without_global_credentials(home):
    (home / "config.json").unlink()
    actor_calls = []
    auth = {"require_actor": actor_calls.append}
    connection = connection_for()
    payload = stack_payload("Frozen stack", "a" * 64)
    record = records.prepare_stack_submission(
        PROJECT, USER, payload, connection=connection.binding(), **auth
    )
    requests = []

    def handler(request):
        requests.append(
            (request.url.path, request.headers["Authorization"], json.loads(request.content))
        )
        if request.url.path.endswith("lookup"):
            return httpx.Response(200, json=_body(record, "absent"))
        return httpx.Response(200, json=_body(record))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpStackTransport(
            "https://example.test",
            client,
            connection=connection,
            credentials=lambda: ActiveCredentials(USER, "session-token"),
            **auth,
        )
        assert submission.recover_stack(record.scope, transport, **auth).status == "absent"
        saved = records.read_stack_submission(record.scope, **auth)
        assert saved.connection == connection.binding()
        assert saved.payload_json == record.payload_json
        assert submission.retry_stack(saved.scope, transport, **auth).status == "committed"
    assert [request[0] for request in requests] == [
        "/stack/lookup",
        "/stack/lookup",
        "/stack/submit",
    ]
    assert {request[1] for request in requests} == {"Bearer session-token"}
    assert all(request[2]["payload_json"] == record.payload_json for request in requests)
    assert set(actor_calls) == {USER}
    assert records.list_stack_submissions(PROJECT, USER, **auth)[0].phase == "resolved"


def test_old_unbound_record_keeps_canonical_encoding(home):
    record = _prepared()
    assert record.connection is None
    assert "connection" not in record.model_dump()
    assert records.read_stack_submission(record.scope) == record


@pytest.mark.parametrize("changed_after_request", [False, True])
def test_session_account_change_never_accepts_response(home, changed_after_request):
    record = _prepared()
    requests = []
    guard = Mock(side_effect=SubmissionActorMismatchError("changed"))

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=_body(record))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpStackTransport(
            "https://example.test",
            client,
            credentials=lambda: ActiveCredentials(
                USER if changed_after_request else OTHER, "token"
            ),
            require_actor=guard,
        )
        with pytest.raises(SubmissionActorMismatchError):
            transport.submit(record)
    assert requests == (["/stack/submit"] if changed_after_request else [])
    assert guard.call_count == (1 if changed_after_request else 0)
    assert records.read_stack_submission(record.scope).phase == "prepared"


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
@pytest.mark.parametrize(
    "saved,active",
    [
        (connection_for("https://first.test"), connection_for("https://second.test")),
        (connection_for("https://first.test"), None),
        (None, connection_for("https://second.test")),
        (
            connection_for("https://second.test", "first-client"),
            connection_for("https://second.test", "second-client"),
        ),
    ],
)
def test_connection_mismatch_never_sends_saved_attempt(home, mode, saved, active):
    record = records.prepare_stack_submission(
        PROJECT, USER, stack_payload("Next?"), connection=saved.binding() if saved else None
    )
    handler = Mock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpStackTransport(
            "https://second.test",
            client,
            connection=active,
            credentials=lambda: ActiveCredentials(USER, "same-user-token"),
        )
        with pytest.raises(StackTransportError, match="connection does not match"):
            getattr(submission, f"{mode}_stack")(record.scope, transport)
    handler.assert_not_called()
    persisted = records.read_stack_submission(record.scope)
    assert persisted.connection == (saved.binding() if saved else None)
    assert persisted.payload_json == record.payload_json
    assert persisted.payload_digest == record.payload_digest
    assert persisted.result is None


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
@pytest.mark.parametrize(
    "connection",
    [None, connection_for("https://first.test"), connection_for("https://second.test")],
)
def test_matching_connection_or_legacy_default_accepts_bound_receipt(home, mode, connection):
    record = records.prepare_stack_submission(
        PROJECT,
        USER,
        stack_payload("Next?"),
        connection=connection.binding() if connection else None,
    )
    calls = []

    def handler(request):
        calls.append((str(request.url), request.headers["Authorization"]))
        return httpx.Response(200, json=_body(record))

    origin = connection.endpoint.removesuffix("/mcp") if connection else "https://legacy.test"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpStackTransport(
            origin,
            client,
            connection=connection,
            credentials=lambda: ActiveCredentials(USER, "same-user-token"),
        )
        result = getattr(submission, f"{mode}_stack")(record.scope, transport)
    assert result.status == "committed"
    route = "submit" if mode == "submit" else "lookup"
    assert calls == [(f"{origin}/stack/{route}", "Bearer same-user-token")]


def test_bound_uncertain_attempt_survives_process_restart(home):
    import subprocess
    import sys

    record = records.prepare_stack_submission(
        PROJECT, USER, stack_payload("Frozen stack", "a" * 64), connection="saved-binding"
    )
    records.mark_stack_uncertain(record)
    (home / "config.json").unlink()
    script = """
import json, sys
from nauro.store.stack_records import list_stack_submissions
record, = list_stack_submissions(sys.argv[1], sys.argv[2], require_actor=lambda actor: None)
print(record.model_dump_json())
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, PROJECT, USER], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    loaded = records.StackSubmission.model_validate_json(completed.stdout)
    assert loaded.connection == "saved-binding"
    assert loaded.scope == record.scope
    assert loaded.created_at == record.created_at
    assert loaded.payload_json == record.payload_json
    assert loaded.payload_digest == record.payload_digest
    assert loaded.phase == "uncertain"


@pytest.mark.parametrize("route", ["submit", "lookup"])
@pytest.mark.parametrize("origin", ["https://other.test", "https://example.test:9443"])
def test_same_saved_binding_cannot_send_to_another_origin(home, route, origin):
    connection = connection_for()
    record = records.prepare_stack_submission(
        PROJECT, USER, stack_payload("Frozen stack"), connection=connection.binding()
    )
    credentials = Mock(return_value=ActiveCredentials(USER, "private-token"))
    handler = Mock()
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(StackTransportError, match="origin does not match"),
    ):
        transport = HttpStackTransport(
            origin, client, connection=connection, credentials=credentials
        )
        getattr(transport, route)(record)
    credentials.assert_not_called()
    handler.assert_not_called()


@pytest.mark.parametrize("origin", ["https://EXAMPLE.test:443", "https://example.test/"])
def test_equivalent_normalized_origin_accepts_bound_receipt(home, origin):
    connection = connection_for()
    record = records.prepare_stack_submission(
        PROJECT, USER, stack_payload("Frozen stack"), connection=connection.binding()
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_body(record)))
    ) as client:
        result = HttpStackTransport(origin, client, connection=connection).submit(record)
    assert result.status == "committed"
