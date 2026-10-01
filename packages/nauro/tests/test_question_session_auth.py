"""Question attempts retain their connection and use the session's actor guard."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.store import question_records as records
from nauro.store.question_contract import question_payload, resolution_payload
from nauro.store.submission_records import SubmissionActorMismatchError
from nauro.sync import question_submission as submission
from nauro.sync.question_transport import HttpQuestionTransport
from tests.test_question_submission import OTHER, PROJECT, USER, _body, _prepared, home

__all__ = ["home"]


@pytest.mark.parametrize("action", ["append", "resolve"])
def test_session_authority_survives_restart_without_global_credentials(home, action):
    (home / "config.json").unlink()
    actor_calls = []
    auth = {"require_actor": actor_calls.append}
    payload = (
        question_payload("Next?") if action == "append" else resolution_payload(("Q1",), "D42")
    )
    record = records.prepare_question_submission(
        PROJECT, USER, payload, connection="endpoint-binding", **auth
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
        transport = HttpQuestionTransport(
            "https://example.test",
            client,
            credentials=lambda: ActiveCredentials(USER, "session-token"),
            **auth,
        )
        assert submission.recover_question(record.scope, transport, **auth).status == "absent"
        saved = records.read_question_submission(record.scope, **auth)
        assert saved.connection == "endpoint-binding"
        assert saved.payload_json == record.payload_json
        assert submission.retry_question(saved.scope, transport, **auth).status == "committed"
    assert [request[0] for request in requests] == [
        "/questions/lookup",
        "/questions/lookup",
        "/questions/submit",
    ]
    assert {request[1] for request in requests} == {"Bearer session-token"}
    assert all(request[2]["payload_json"] == record.payload_json for request in requests)
    assert set(actor_calls) == {USER}
    assert records.list_question_submissions(PROJECT, USER, **auth)[0].phase == "resolved"


def test_old_unbound_record_keeps_canonical_encoding(home):
    record = _prepared()
    assert record.connection is None
    assert "connection" not in record.model_dump()
    assert records.read_question_submission(record.scope) == record


@pytest.mark.parametrize("changed_after_request", [False, True])
def test_session_account_change_never_accepts_response(home, changed_after_request):
    record = _prepared()
    requests = []
    guard = Mock(side_effect=SubmissionActorMismatchError("changed"))

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=_body(record))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpQuestionTransport(
            "https://example.test",
            client,
            credentials=lambda: ActiveCredentials(
                USER if changed_after_request else OTHER, "token"
            ),
            require_actor=guard,
        )
        with pytest.raises(SubmissionActorMismatchError):
            transport.submit(record)
    assert requests == (["/questions/submit"] if changed_after_request else [])
    assert guard.call_count == (1 if changed_after_request else 0)
    assert records.read_question_submission(record.scope).phase == "prepared"
