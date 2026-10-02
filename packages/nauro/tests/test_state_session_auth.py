"""State requests verify saved connection authority before changing uncertainty."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.store import state_records as records
from nauro.store.state_contract import StateTransportError, state_payload
from nauro.sync import state_submission as submission
from nauro.sync.generation_credentials import GenerationConnection
from nauro.sync.state_transport import HttpStateTransport
from tests.test_state_submission import PROJECT, USER, _body, home

__all__ = ["home"]


def connection_for(client_id="client"):
    return GenerationConnection(
        endpoint="https://example.test/mcp",
        issuer="https://issuer.test/",
        client_id=client_id,
        audience="https://api.test",
        redirect_uri="http://127.0.0.1:8080/callback",
    )


@pytest.mark.parametrize(
    "origin,provider",
    [
        ("https://other.test", True),
        ("https://example.test:9443", True),
        ("https://example.test", False),
    ],
)
def test_bound_origin_and_provider_required_before_io(home, monkeypatch, origin, provider):
    from nauro.sync import state_transport

    credentials, handler = Mock(), Mock()
    monkeypatch.setattr(state_transport, "read_active_credentials", credentials)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(StateTransportError),
    ):
        HttpStateTransport(
            origin,
            client,
            connection=connection_for(),
            credentials=credentials if provider else None,
        )
    credentials.assert_not_called()
    handler.assert_not_called()


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
@pytest.mark.parametrize("saved_bound,active_bound", [(True, True), (True, False), (False, True)])
def test_binding_refusal_preserves_complete_record(home, mode, saved_bound, active_bound):
    saved_connection = connection_for()
    record = records.prepare_state_submission(
        PROJECT,
        USER,
        state_payload("Frozen state", "a" * 64),
        connection=saved_connection.binding() if saved_bound else None,
    )
    if mode != "submit":
        record = records.mark_state_uncertain(record)
    credentials, handler = Mock(), Mock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpStateTransport(
            "https://example.test",
            client,
            connection=connection_for("other") if active_bound else None,
            credentials=credentials,
        )
        with pytest.raises(StateTransportError, match="connection does not match"):
            getattr(submission, f"{mode}_state")(record.scope, transport)
        assert records.read_state_submission(record.scope) == record
        credentials.assert_not_called()
        handler.assert_not_called()
        if mode == "submit":
            credentials.return_value = ActiveCredentials(USER, "session-token")
            handler.return_value = httpx.Response(200, json=_body(record))
            corrected = HttpStateTransport(
                "https://example.test",
                client,
                connection=saved_connection if saved_bound else None,
                credentials=credentials,
            )
            assert submission.submit_state(record.scope, corrected).status == "committed"
            body = json.loads(handler.call_args.args[0].content)
            assert body["payload_json"] == record.payload_json
            assert body["operation_id"] == record.scope.operation_id
            assert body["payload_digest"] == record.payload_digest
            assert records.read_state_submission(record.scope).created_at == record.created_at


@pytest.mark.parametrize("origin", ["https://EXAMPLE.test:443", "https://example.test/"])
def test_matching_normalized_origin_uses_explicit_provider(home, origin):
    connection = connection_for()
    record = records.prepare_state_submission(
        PROJECT, USER, state_payload("Frozen"), connection=connection.binding()
    )
    (home / "config.json").unlink()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=_body(record))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        transport = HttpStateTransport(
            origin,
            client,
            connection=connection,
            credentials=lambda: ActiveCredentials(USER, "session-token"),
            require_actor=lambda actor: None,
        )
        assert (
            submission.submit_state(
                record.scope, transport, require_actor=lambda actor: None
            ).status
            == "committed"
        )
    assert requests[0].headers["Authorization"] == "Bearer session-token"
