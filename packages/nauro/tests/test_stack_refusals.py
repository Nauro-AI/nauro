"""Closed HTTP refusals preserve evidence about the original saved write."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.mcp import stack_responses
from nauro.store import stack_records as records
from nauro.store.stack_contract import StackTransportError
from nauro.sync import stack_submission as submission
from nauro.sync.stack_transport import HttpStackTransport
from tests.test_stack_submission import _body, _prepared, home

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
        transport = HttpStackTransport("https://example.test", client)
        result = submission.submit_stack(record.scope, transport)
        assert result.status == "refused"
        assert result.server_code == code
        assert result.http_status == status
        assert result.request_mode == "submit"
        assert result.unresolved is False
        saved = records.read_stack_submission(record.scope)
        assert saved.result == result
        assert saved.phase == "resolved"
        assert saved.scope == record.scope
        assert saved.payload_digest == record.payload_digest
        for mode in (
            submission.submit_stack,
            submission.retry_stack,
            submission.recover_stack,
        ):
            assert mode(record.scope, transport) == result
    assert calls == ["/stack/submit"]


@pytest.mark.parametrize(
    "status,code", [(403, "project_not_selected"), (409, "single_writer_refused")]
)
@pytest.mark.parametrize("mode", [submission.recover_stack, submission.retry_stack])
def test_lookup_refusal_retains_uncertainty_until_authority_returns(home, status, code, mode):
    record = _prepared()
    with pytest.raises(StackTransportError):
        submission.submit_stack(record.scope, Mock(submit=Mock(side_effect=StackTransportError())))
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": code})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = mode(record.scope, HttpStackTransport("https://example.test", client))
    assert result.status == "refused"
    assert result.server_code == code
    assert result.http_status == status
    assert result.request_mode == "lookup"
    assert result.unresolved is True
    saved = records.read_stack_submission(record.scope)
    assert saved.phase == "uncertain"
    assert saved.result == result
    assert calls == ["/stack/lookup"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_body(record)))
    ) as client:
        recovered = submission.recover_stack(
            record.scope, HttpStackTransport("https://example.test", client)
        )
    assert recovered.status == "committed"
    assert records.read_stack_submission(record.scope).phase == "resolved"


@pytest.mark.parametrize("status,code", [(403, "forbidden"), (400, "invalid_request")])
def test_refusal_that_can_follow_publication_remains_uncertain(home, status, code):
    record = _prepared()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": code}))
    ) as client:
        result = submission.submit_stack(
            record.scope, HttpStackTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_stack_submission(record.scope).phase == "uncertain"


def test_retry_refusal_does_not_erase_original_unknown_send(home):
    record = _prepared()
    records.mark_stack_uncertain(record)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("lookup"):
            return httpx.Response(200, json=_body(record, "absent"))
        return httpx.Response(409, json={"detail": "single_writer_refused"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = submission.retry_stack(
            record.scope, HttpStackTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_stack_submission(record.scope).phase == "uncertain"
    assert calls == ["/stack/lookup", "/stack/submit"]


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
        pytest.raises(StackTransportError),
    ):
        getattr(submission, "submit_stack" if route == "submit" else "recover_stack")(
            record.scope, HttpStackTransport("https://example.test", client)
        )
    assert calls == [f"/stack/{route}"]
    assert records.read_stack_submission(record.scope).phase == (
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
        pytest.raises(StackTransportError),
    ):
        submission.submit_stack(record.scope, HttpStackTransport("https://example.test", client))
    assert records.read_stack_submission(record.scope).phase == "uncertain"


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
def test_dormant_response_adapter_preserves_refusal_evidence(home, mode):
    record = _prepared()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"detail": "project_not_selected"})
        )
    ) as client:
        response = getattr(stack_responses, f"{mode}_stack")(
            record.scope, HttpStackTransport("https://example.test", client)
        )
    assert response.isError is True
    assert response.structuredContent["status"] == "refused"
    assert response.structuredContent["server_code"] == "project_not_selected"
    assert response.structuredContent["unresolved"] is (mode != "submit")
    assert response.content[0].text == "Stack request refused: project_not_selected. " + (
        "This attempt did not write. Correct the refusal before a new attempt."
        if mode == "submit"
        else (
            "The original write outcome remains unknown. Restore access and recover this reference."
        )
    )
