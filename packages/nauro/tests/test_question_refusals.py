"""Closed HTTP refusals preserve evidence about the original saved write."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.mcp import question_responses
from nauro.store import question_records as records
from nauro.store.question_contract import (
    QuestionRefused,
    QuestionTransportError,
    verify_question_response,
)
from nauro.sync import question_submission as submission
from nauro.sync.question_transport import HttpQuestionTransport
from tests.test_question_submission import _body, _prepared, home

__all__ = ["home"]


@pytest.mark.parametrize(
    "status,code", [(403, "project_not_selected"), (409, "single_writer_refused")]
)
@pytest.mark.parametrize("action", ["append", "resolve"])
def test_first_submit_refusal_is_durable_and_terminal(home, action, status, code):
    record = _prepared(action)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": code})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpQuestionTransport("https://example.test", client)
        result = submission.submit_question(record.scope, transport)
        assert result.status == "refused"
        assert result.server_code == code
        assert result.http_status == status
        assert result.request_mode == "submit"
        assert result.unresolved is False
        saved = records.read_question_submission(record.scope)
        assert saved.result == result
        assert saved.phase == "resolved"
        assert saved.scope == record.scope
        assert saved.payload_digest == record.payload_digest
        for mode in (
            submission.submit_question,
            submission.retry_question,
            submission.recover_question,
        ):
            assert mode(record.scope, transport) == result
    assert calls == ["/questions/submit"]


@pytest.mark.parametrize(
    "status,code", [(403, "project_not_selected"), (409, "single_writer_refused")]
)
@pytest.mark.parametrize("mode", [submission.recover_question, submission.retry_question])
@pytest.mark.parametrize("action", ["append", "resolve"])
def test_lookup_refusal_retains_uncertainty_until_authority_returns(
    home, action, status, code, mode
):
    record = _prepared(action)
    with pytest.raises(QuestionTransportError):
        submission.submit_question(
            record.scope, Mock(submit=Mock(side_effect=QuestionTransportError()))
        )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": code})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = mode(record.scope, HttpQuestionTransport("https://example.test", client))
    assert result.status == "refused"
    assert result.server_code == code
    assert result.http_status == status
    assert result.request_mode == "lookup"
    assert result.unresolved is True
    saved = records.read_question_submission(record.scope)
    assert saved.phase == "uncertain"
    assert saved.result == result
    assert calls == ["/questions/lookup"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_body(record)))
    ) as client:
        recovered = submission.recover_question(
            record.scope, HttpQuestionTransport("https://example.test", client)
        )
    assert recovered.status == "committed"
    assert records.read_question_submission(record.scope).phase == "resolved"


@pytest.mark.parametrize("status,code", [(403, "forbidden"), (400, "invalid_request")])
@pytest.mark.parametrize("action", ["append", "resolve"])
def test_refusal_that_can_follow_publication_remains_uncertain(home, action, status, code):
    record = _prepared(action)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": code}))
    ) as client:
        result = submission.submit_question(
            record.scope, HttpQuestionTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_question_submission(record.scope).phase == "uncertain"


@pytest.mark.parametrize("action", ["append", "resolve"])
def test_retry_refusal_does_not_erase_original_unknown_send(home, action):
    record = _prepared(action)
    records.mark_question_uncertain(record)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("lookup"):
            return httpx.Response(200, json=_body(record, "absent"))
        return httpx.Response(409, json={"detail": "single_writer_refused"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = submission.retry_question(
            record.scope, HttpQuestionTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_question_submission(record.scope).phase == "uncertain"
    assert calls == ["/questions/lookup", "/questions/submit"]


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
@pytest.mark.parametrize("action", ["append", "resolve"])
def test_uncontracted_response_is_not_a_refusal(home, action, route, status, raw):
    record = _prepared(action)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, content=raw, headers={"Location": "https://elsewhere.test"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client,
        pytest.raises(QuestionTransportError),
    ):
        getattr(submission, "submit_question" if route == "submit" else "recover_question")(
            record.scope, HttpQuestionTransport("https://example.test", client)
        )
    assert calls == [f"/questions/{route}"]
    assert records.read_question_submission(record.scope).phase == (
        "uncertain" if route == "submit" else "prepared"
    )


@pytest.mark.parametrize("action", ["append", "resolve"])
def test_http_success_cannot_inject_local_refusal_evidence(home, action):
    record = _prepared(action)
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
        pytest.raises(QuestionTransportError),
    ):
        submission.submit_question(
            record.scope, HttpQuestionTransport("https://example.test", client)
        )
    assert records.read_question_submission(record.scope).phase == "uncertain"


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
@pytest.mark.parametrize("action", ["append", "resolve"])
def test_dormant_response_adapter_preserves_refusal_evidence(home, mode, action):
    record = _prepared(action)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"detail": "project_not_selected"})
        )
    ) as client:
        response = getattr(question_responses, f"{mode}_question")(
            record.scope, HttpQuestionTransport("https://example.test", client)
        )
    assert response.isError is True
    assert response.structuredContent["status"] == "refused"
    assert response.structuredContent["server_code"] == "project_not_selected"
    assert response.structuredContent["unresolved"] is (mode != "submit")
    assert response.content[0].text == "Question request refused: project_not_selected. " + (
        "This attempt did not write. Correct the refusal before a new attempt."
        if mode == "submit"
        else (
            "The original write outcome remains unknown. Restore access and recover this reference."
        )
    )


@pytest.mark.parametrize("action", ["append", "resolve"])
@pytest.mark.parametrize("mode", ["submit", "retry"])
def test_custom_transport_cannot_supply_terminal_refusal(home, mode, action):
    record = _prepared(action)
    if mode == "retry":
        records.mark_question_uncertain(record)
    refusal = QuestionRefused(
        version=1,
        scope=record.scope,
        payload_digest=record.payload_digest,
        action=action,
        http_status=403,
        server_code="actor_mismatch",
        request_mode="submit",
        unresolved=False,
    )
    absent = verify_question_response(
        json.dumps(_body(record, "absent")).encode(), record.scope, record.payload_json, lookup=True
    )
    transport = Mock(
        spec=["submit", "lookup"],
        lookup=Mock(return_value=absent),
        submit=Mock(return_value=refusal),
    )
    with pytest.raises(QuestionTransportError, match="terminal refusal"):
        getattr(submission, f"{mode}_question")(record.scope, transport)
    saved = records.read_question_submission(record.scope)
    assert saved.phase == "uncertain"
    assert saved.result == (absent if mode == "retry" else None)
