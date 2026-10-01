"""Closed HTTP refusals preserve evidence about the original saved write."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.store import state_records as records
from nauro.store.state_contract import StateTransportError
from nauro.sync import state_submission as submission
from nauro.sync.state_transport import HttpStateTransport
from tests.test_state_submission import _body, _prepared, home

__all__ = ["home"]


@pytest.mark.parametrize(
    "status,code", [(403, "project_not_selected"), (409, "single_writer_refused")]
)
def test_first_submit_refusal_is_durable_and_terminal(home, status, code):
    record = _prepared()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": code})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpStateTransport("https://example.test", client)
        result = submission.submit_state(record.scope, transport)
        assert result.status == "refused"
        assert result.server_code == code
        assert result.http_status == status
        assert result.request_mode == "submit"
        assert result.unresolved is False
        saved = records.read_state_submission(record.scope)
        assert saved.result == result
        assert saved.phase == "resolved"
        assert saved.scope == record.scope
        assert saved.payload_digest == record.payload_digest
        for mode in (submission.submit_state, submission.retry_state, submission.recover_state):
            assert mode(record.scope, transport) == result
    assert calls == ["/state/submit"]


@pytest.mark.parametrize(
    "status,code", [(403, "project_not_selected"), (409, "single_writer_refused")]
)
@pytest.mark.parametrize("mode", [submission.recover_state, submission.retry_state])
def test_lookup_refusal_retains_uncertainty_until_authority_returns(home, status, code, mode):
    record = _prepared()
    with pytest.raises(StateTransportError):
        submission.submit_state(record.scope, Mock(submit=Mock(side_effect=StateTransportError())))
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": code})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = mode(record.scope, HttpStateTransport("https://example.test", client))
    assert result.status == "refused"
    assert result.server_code == code
    assert result.http_status == status
    assert result.request_mode == "lookup"
    assert result.unresolved is True
    saved = records.read_state_submission(record.scope)
    assert saved.phase == "uncertain"
    assert saved.result == result
    assert calls == ["/state/lookup"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_body(record)))
    ) as client:
        recovered = submission.recover_state(
            record.scope, HttpStateTransport("https://example.test", client)
        )
    assert recovered.status == "committed"
    assert records.read_state_submission(record.scope).phase == "resolved"


@pytest.mark.parametrize("status,code", [(403, "forbidden"), (400, "invalid_request")])
def test_refusal_that_can_follow_publication_remains_uncertain(home, status, code):
    record = _prepared()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": code}))
    ) as client:
        result = submission.submit_state(
            record.scope, HttpStateTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_state_submission(record.scope).phase == "uncertain"


def test_retry_refusal_does_not_erase_original_unknown_send(home):
    record = _prepared()
    records.mark_state_uncertain(record)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("lookup"):
            return httpx.Response(200, json=_body(record, "absent"))
        return httpx.Response(409, json={"detail": "single_writer_refused"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = submission.retry_state(
            record.scope, HttpStateTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_state_submission(record.scope).phase == "uncertain"
    assert calls == ["/state/lookup", "/state/submit"]


@pytest.mark.parametrize("route", ["submit", "lookup"])
@pytest.mark.parametrize(
    "status,raw",
    [
        (403, b'{"detail":"unknown_refusal"}'),
        (409, b'{"detail":"project_not_selected"}'),
        (403, b'{"detail":"forbidden","extra":true}'),
        (403, b'{"detail":"forbidden","detail":"project_not_selected"}'),
        (403, b'{"detail":null}'),
        (403, b"not json"),
        (503, b'{"detail":"single_writer_refused"}'),
        (302, b'{"detail":"project_not_selected"}'),
    ],
)
def test_uncontracted_response_is_not_a_refusal(home, route, status, raw):
    record = _prepared()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, content=raw, headers={"Location": "https://elsewhere.test"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client,
        pytest.raises(StateTransportError),
    ):
        getattr(submission, "submit_state" if route == "submit" else "recover_state")(
            record.scope, HttpStateTransport("https://example.test", client)
        )
    assert calls == [f"/state/{route}"]
    assert records.read_state_submission(record.scope).phase == (
        "uncertain" if route == "submit" else "prepared"
    )


def test_http_success_cannot_inject_local_refusal_evidence(home):
    record = _prepared()
    body = {
        **_body(record, "absent"),
        "status": "refused",
        "unresolved": False,
        "http_status": 409,
        "server_code": "single_writer_refused",
        "request_mode": "submit",
    }
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=json.dumps(body))
            )
        ) as client,
        pytest.raises(StateTransportError),
    ):
        submission.submit_state(record.scope, HttpStateTransport("https://example.test", client))
    assert records.read_state_submission(record.scope).phase == "uncertain"
