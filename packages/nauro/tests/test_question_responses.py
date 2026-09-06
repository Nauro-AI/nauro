"""Truthful MCP question responses through the durable coordinator."""

import json
from unittest.mock import Mock, call

import pytest
from filelock import Timeout

from nauro.auth import ActiveUserReadError
from nauro.mcp import question_responses as responses
from nauro.store import question_records as records
from nauro.store.question_contract import QuestionTransportError, verify_question_response
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync import question_submission
from tests.test_question_submission import OTHER, _body, _prepared, _result, home

__all__ = ["home"]


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (
            "absent",
            "No receipt was found. This operation remains unresolved; an earlier request may "
            "still commit. Recovery only checks for a receipt.",
        ),
        (
            "no_change_observed",
            "This attempt made no question resolution change. This operation remains "
            "unresolved; an earlier request may still commit. Recover the original operation.",
        ),
        (
            "expired",
            "The receipt replay window has expired. This does not prove that no write occurred. "
            "Do not resend this operation.",
        ),
        (
            "digest_conflict",
            "This operation identity is bound to a different payload. "
            "Do not resend this operation with changed content.",
        ),
    ],
)
def test_noncommit_keeps_exact_evidence_and_truthful_text(home, status, text):
    record = _prepared("resolve")
    result = _result(record, status)
    transport = Mock(submit=Mock(return_value=result), lookup=Mock(return_value=result))
    action = responses.recover_question if status == "absent" else responses.submit_question
    response = action(record.scope, transport)
    assert response.isError is True
    assert response.structuredContent == result.model_dump(mode="json")
    assert response.content[0].text == text
    assert records.read_question_submission(record.scope).result == result


def test_observation_then_commit_and_cached_receipt(home):
    status = "no_change_observed"
    record = _prepared("resolve")
    body = _body(record)
    committed = verify_question_response(
        json.dumps(body).encode(), record.scope, record.payload_json
    )
    transport = Mock(
        submit=Mock(return_value=_result(record, status)), lookup=Mock(return_value=committed)
    )
    assert (
        responses.submit_question(record.scope, transport).structuredContent["unresolved"] is True
    )
    response = responses.recover_question(record.scope, transport)
    assert response.isError is False
    assert response.structuredContent == body
    assert response.content[0].text == (
        "Question operation committed. This receipt does not establish current local state."
    )
    assert responses.recover_question(record.scope, transport) == response
    assert responses.retry_question(record.scope, transport) == response
    assert responses.submit_question(record.scope, transport) == response
    assert transport.method_calls == [
        call.submit(
            records.QuestionSubmission.model_validate({**record.model_dump(), "phase": "uncertain"})
        ),
        call.lookup(
            records.QuestionSubmission.model_validate(
                {**record.model_dump(), "phase": "uncertain", "result": _result(record, status)}
            )
        ),
    ]


@pytest.mark.parametrize("action", ["append", "resolve"])
def test_retry_looks_up_before_sending_exact_saved_identity(home, action):
    record = _prepared(action)
    sent = []

    def send(saved):
        sent.append(saved)
        return _result(record)

    transport = Mock(
        lookup=Mock(return_value=_result(record, "absent")), submit=Mock(side_effect=send)
    )
    response = responses.retry_question(record.scope, transport)
    assert response.isError is False
    assert response.structuredContent == _body(record)
    assert [event[0] for event in transport.method_calls] == ["lookup", "submit"]
    assert sent[0].scope == record.scope
    assert sent[0].payload_json == record.payload_json
    assert sent[0].payload_digest == record.payload_digest
    assert sent[0].phase == "uncertain"


def test_recovery_never_submits_and_repeat_submit_does_not_resend(home):
    record = _prepared("resolve")
    transport = Mock(submit=Mock(return_value=_result(record, "no_change_observed")))
    responses.submit_question(record.scope, transport)
    blocked = responses.submit_question(record.scope, transport)
    assert blocked.isError is True
    assert blocked.structuredContent is None
    transport.lookup.return_value = _result(record, "absent")
    assert (
        responses.recover_question(record.scope, transport).structuredContent["unresolved"] is True
    )
    assert transport.submit.call_count == 1
    assert transport.lookup.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        SubmissionRecordError("private record path"),
        QuestionTransportError("secret response"),
        ActiveUserReadError("account detail"),
        OSError("private path"),
        Timeout("private lock"),
    ],
)
def test_known_failure_does_not_disclose_or_claim_outcome(home, failure):
    record = _prepared()
    transport = Mock(submit=Mock(side_effect=failure))
    response = responses.submit_question(record.scope, transport)
    assert response.isError is True
    assert response.structuredContent is None
    assert response.content[0].text == (
        "Question operation outcome is unconfirmed. Keep the saved submission "
        "and recover the original operation when access is available. "
        "Do not infer that no write occurred."
    )
    assert records.read_question_submission(record.scope).phase == "uncertain"
    transport.lookup.assert_not_called()


def test_failed_receipt_persistence_does_not_render_success(home, monkeypatch):
    record = _prepared()
    transport = Mock(submit=Mock(return_value=_result(record)))
    monkeypatch.setattr(
        question_submission, "record_question_result", Mock(side_effect=OSError("final barrier"))
    )
    response = responses.submit_question(record.scope, transport)
    assert response.isError is True
    assert response.structuredContent is None
    assert records.read_question_submission(record.scope).phase == "uncertain"


def test_invalid_receipt_is_not_rendered(home):
    record = _prepared()
    invalid = _result(record).model_copy(update={"receipt_json": "secret invalid evidence"})
    response = responses.submit_question(record.scope, Mock(submit=Mock(return_value=invalid)))
    assert response.isError is True
    assert response.structuredContent is None
    assert records.read_question_submission(record.scope).phase == "uncertain"


def test_unknown_programming_failure_propagates(home):
    record = _prepared()
    with pytest.raises(TypeError, match="implementation defect"):
        responses.submit_question(
            record.scope, Mock(submit=Mock(side_effect=TypeError("implementation defect")))
        )


def test_actor_change_after_transport_does_not_disclose_receipt(home):
    record = _prepared()
    result = _result(record)

    def change_account(saved):
        (home / "config.json").write_text(
            json.dumps({"auth": {"user_id": OTHER, "access_token": "synthetic-test-token"}})
        )
        return result

    transport = Mock(submit=Mock(side_effect=change_account))
    response = responses.submit_question(record.scope, transport)
    assert response.isError is True
    assert response.structuredContent is None
    assert result.receipt_json not in response.content[0].text
    transport.lookup.assert_not_called()
