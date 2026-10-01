"""Share attempts retain their connection and use the session's actor guard."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.store import share_records as records
from nauro.store.share_contract import (
    ShareTransportError,
    share_payload,
)
from nauro.store.submission_records import SubmissionActorMismatchError
from nauro.sync import share_submission as submission
from nauro.sync.generation_credentials import GenerationConnection
from nauro.sync.share_transport import HttpShareTransport
from tests.test_share_submission import OTHER, PROJECT, USER, _body, _prepared, home

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
    payload = share_payload("frozen-brief", "Exact content", "brief", "Exact summary")
    record = records.prepare_share_submission(
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
        transport = HttpShareTransport(
            "https://example.test",
            client,
            connection=connection,
            credentials=lambda: ActiveCredentials(USER, "session-token"),
            **auth,
        )
        assert submission.recover_share(record.scope, transport, **auth).status == "absent"
        saved = records.read_share_submission(record.scope, **auth)
        assert saved.connection == connection.binding()
        assert saved.payload_json == record.payload_json
        assert submission.retry_share(saved.scope, transport, **auth).status == "committed"
    assert [request[0] for request in requests] == [
        "/share/lookup",
        "/share/lookup",
        "/share/submit",
    ]
    assert {request[1] for request in requests} == {"Bearer session-token"}
    assert all(request[2]["payload_json"] == record.payload_json for request in requests)
    assert set(actor_calls) == {USER}
    assert records.list_share_submissions(PROJECT, USER, **auth)[0].phase == "resolved"


def test_old_unbound_record_keeps_canonical_encoding(home):
    record = _prepared()
    assert record.connection is None
    assert "connection" not in record.model_dump()
    assert records.read_share_submission(record.scope) == record


@pytest.mark.parametrize("changed_after_request", [False, True])
def test_session_account_change_never_accepts_response(home, changed_after_request):
    record = _prepared()
    requests = []
    guard = Mock(side_effect=SubmissionActorMismatchError("changed"))

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=_body(record))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpShareTransport(
            "https://example.test",
            client,
            credentials=lambda: ActiveCredentials(
                USER if changed_after_request else OTHER, "token"
            ),
            require_actor=guard,
        )
        with pytest.raises(SubmissionActorMismatchError):
            transport.submit(record)
    assert requests == (["/share/submit"] if changed_after_request else [])
    assert guard.call_count == (1 if changed_after_request else 0)
    assert records.read_share_submission(record.scope).phase == "prepared"


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
    record = records.prepare_share_submission(
        PROJECT,
        USER,
        share_payload("next-brief", "Next content", "brief", "Next summary"),
        connection=saved.binding() if saved else None,
    )
    if mode == "retry":
        record = records.mark_share_uncertain(record)
    handler = Mock()
    credentials = Mock(return_value=ActiveCredentials(USER, "same-user-token"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpShareTransport(
            "https://second.test",
            client,
            connection=active,
            credentials=credentials,
        )
        with pytest.raises(ShareTransportError, match="connection does not match"):
            getattr(submission, f"{mode}_share")(record.scope, transport)
    handler.assert_not_called()
    credentials.assert_not_called()
    persisted = records.read_share_submission(record.scope)
    assert persisted.connection == (saved.binding() if saved else None)
    assert persisted.payload_json == record.payload_json
    assert persisted.payload_digest == record.payload_digest
    assert persisted == record
    if mode == "submit":
        origin = saved.endpoint.removesuffix("/mcp") if saved else "https://legacy.test"
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_body(record)))
        ) as client:
            correct = HttpShareTransport(origin, client, connection=saved, credentials=credentials)
            assert submission.submit_share(record.scope, correct).status == "committed"


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
@pytest.mark.parametrize(
    "connection",
    [None, connection_for("https://first.test"), connection_for("https://second.test")],
)
def test_matching_connection_or_legacy_default_accepts_bound_receipt(home, mode, connection):
    record = records.prepare_share_submission(
        PROJECT,
        USER,
        share_payload("next-brief", "Next content", "brief", "Next summary"),
        connection=connection.binding() if connection else None,
    )
    calls = []

    def handler(request):
        calls.append((str(request.url), request.headers["Authorization"]))
        return httpx.Response(200, json=_body(record))

    origin = connection.endpoint.removesuffix("/mcp") if connection else "https://legacy.test"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpShareTransport(
            origin,
            client,
            connection=connection,
            credentials=lambda: ActiveCredentials(USER, "same-user-token"),
        )
        result = getattr(submission, f"{mode}_share")(record.scope, transport)
    assert result.status == "committed"
    route = "submit" if mode == "submit" else "lookup"
    assert calls == [(f"{origin}/share/{route}", "Bearer same-user-token")]


def test_bound_uncertain_attempt_survives_process_restart(home):
    import subprocess
    import sys

    record = records.prepare_share_submission(
        PROJECT,
        USER,
        share_payload("frozen-brief", "Exact content", "brief", "Exact summary"),
        connection="saved-binding",
    )
    records.mark_share_uncertain(record)
    (home / "config.json").unlink()
    script = """
import json, sys
from nauro.store.share_records import list_share_submissions
record, = list_share_submissions(sys.argv[1], sys.argv[2], require_actor=lambda actor: None)
print(record.model_dump_json())
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, PROJECT, USER], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    loaded = records.ShareSubmission.model_validate_json(completed.stdout)
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
    record = records.prepare_share_submission(
        PROJECT,
        USER,
        share_payload("frozen-brief", "Exact content", "brief", "Exact summary"),
        connection=connection.binding(),
    )
    credentials = Mock(return_value=ActiveCredentials(USER, "private-token"))
    handler = Mock()
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ShareTransportError, match="origin does not match"),
    ):
        transport = HttpShareTransport(
            origin, client, connection=connection, credentials=credentials
        )
        getattr(transport, route)(record)
    credentials.assert_not_called()
    handler.assert_not_called()


@pytest.mark.parametrize("origin", ["https://EXAMPLE.test:443", "https://example.test/"])
def test_equivalent_normalized_origin_accepts_bound_receipt(home, origin):
    connection = connection_for()
    record = records.prepare_share_submission(
        PROJECT,
        USER,
        share_payload("frozen-brief", "Exact content", "brief", "Exact summary"),
        connection=connection.binding(),
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_body(record)))
    ) as client:
        result = HttpShareTransport(
            origin,
            client,
            connection=connection,
            credentials=lambda: ActiveCredentials(USER, "session-token"),
        ).submit(record)
    assert result.status == "committed"


def test_bound_transport_requires_an_explicit_credentials_provider():
    with httpx.Client() as client, pytest.raises(ShareTransportError, match="credentials provider"):
        HttpShareTransport("https://example.test", client, connection=connection_for())
