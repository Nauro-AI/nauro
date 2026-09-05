"""Client transport refuses unbound evidence and preserves uncertain records."""

import json

import httpx
import pytest

from nauro.auth import read_active_credentials
from nauro.store.submission_records import read_submission
from nauro.sync.judgment_submission import recover_judgment, submit_judgment
from nauro.sync.judgment_transport import (
    HttpJudgmentTransport,
    JudgmentTransportError,
    verify_judgment_response,
)
from tests.test_judgment_submission import OTHER, USER, _prepared, home

__all__ = ["home"]


def _response(record, **changes):
    body = {
        "version": 1,
        "scope": record.scope.model_dump(),
        "payload_digest": record.payload_digest,
        "status": "absent",
        "receipt_json": None,
    }
    body.update(changes)
    return json.dumps(body).encode()


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"unexpected": "field"},
        {"payload_digest": "a" * 64},
        {"status": "committed"},
        {"receipt_json": "{}"},
        {"status": "missing"},
    ],
)
def test_response_binding_fails_closed(home, changes):
    record = _prepared()
    with pytest.raises(JudgmentTransportError):
        verify_judgment_response(_response(record, **changes), record.scope, record.payload_digest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", OTHER),
        ("project_id", OTHER),
        ("operation_id", "other-operation"),
        ("operation_kind", "update_state"),
    ],
)
def test_response_scope_must_match(home, field, value):
    record = _prepared()
    scope = {**record.scope.model_dump(), field: value}
    with pytest.raises(JudgmentTransportError):
        verify_judgment_response(
            _response(record, scope=scope), record.scope, record.payload_digest
        )


@pytest.mark.parametrize("raw", [b'{"version":1,"version":1}', b"NaN", b"\xff", b" " * 65537])
def test_malformed_response_refused(home, raw):
    record = _prepared()
    with pytest.raises(JudgmentTransportError):
        verify_judgment_response(raw, record.scope, record.payload_digest)


@pytest.mark.parametrize("status", [301, 307, 401, 403, 404, 409, 429, 500, 503])
def test_http_failure_does_not_resend_or_resolve(home, status):
    record = _prepared()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example/"})

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        transport = HttpJudgmentTransport("https://example.test", client)
        with pytest.raises(JudgmentTransportError):
            submit_judgment(record.scope, transport)
    assert len(requests) == 1
    assert read_submission(record.scope).phase == "uncertain"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-test-token"
    assert json.loads(requests[0].content)["approved_payload"] == record.approved_payload


def test_lost_response_is_followed_only_by_lookup(home):
    record = _prepared()
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/judgments/submit":
            raise httpx.ReadError("lost response")
        return httpx.Response(200, content=_response(record, status="pending"))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = HttpJudgmentTransport("https://example.test", client)
        with pytest.raises(JudgmentTransportError):
            submit_judgment(record.scope, transport)
        assert recover_judgment(record.scope, transport).status == "pending"
    assert [request.url.path for request in requests] == ["/judgments/submit", "/judgments/lookup"]
    assert "approved_payload" not in json.loads(requests[1].content)
    assert read_submission(record.scope).phase == "uncertain"


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.test",
        "https://user:secret@example.test",
        "https://example.test/path",
        "https://example.test/?token=x",
        "https://example.test/#fragment",
    ],
)
def test_unsafe_origin_refused(origin):
    with httpx.Client() as client, pytest.raises(JudgmentTransportError):
        HttpJudgmentTransport(origin, client)


def test_credentials_bind_actor_and_token_from_one_read(home):
    credentials = read_active_credentials()
    assert credentials.user_id == USER
    assert credentials.access_token == "synthetic-test-token"
    assert "synthetic-test-token" not in repr(credentials)


def _receipt(record):
    generation = "01K00000000000000000000009"
    return {
        "receipt_id": generation,
        "operation_kind": "judgment_commit",
        "operation_id": record.scope.operation_id,
        "result": "committed",
        "committed_at": "2026-09-05T00:00:00.000000Z",
        "artifact_digest": "a" * 64,
        "generation_id": generation,
        "target_kind": "decision",
        "target_id": "005-durable-identity",
        "details": {
            "decision_counter": 5,
            "fencing_token": 1,
            "manifest_digest": "a" * 64,
            "plan_record_digest": "b" * 64,
            "saga_id": generation,
            "snapshot_digest": "c" * 64,
            "snapshot_key": f"generations/{record.scope.project_id}/{generation}/snapshot.json",
        },
    }


def _encoded_receipt(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_exact_canonical_receipt_is_retained(home):
    record = _prepared()
    receipt = _encoded_receipt(_receipt(record))
    result = verify_judgment_response(
        _response(record, status="committed", receipt_json=receipt),
        record.scope,
        record.payload_digest,
    )
    assert result.receipt_json == receipt


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "other-operation"),
        ("operation_kind", "update_state"),
        ("result", "accepted"),
        ("artifact_digest", "b" * 64),
        ("generation_id", "01K00000000000000000000008"),
        ("target_kind", "question"),
        ("receipt_id", "invalid"),
        ("committed_at", "yesterday"),
        ("extra", "unexpected"),
    ],
)
def test_receipt_fields_are_bound(home, field, value):
    record = _prepared()
    receipt = {**_receipt(record), field: value}
    with pytest.raises(JudgmentTransportError):
        verify_judgment_response(
            _response(record, status="committed", receipt_json=_encoded_receipt(receipt)),
            record.scope,
            record.payload_digest,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision_counter", True),
        ("decision_counter", "5"),
        ("fencing_token", 0),
        ("snapshot_key", "generations/other/snapshot.json"),
        ("saga_id", "invalid"),
        ("manifest_digest", "b" * 64),
        ("plan_record_digest", "invalid"),
        ("extra", "unexpected"),
    ],
)
def test_receipt_details_are_validated(home, field, value):
    record = _prepared()
    receipt = _receipt(record)
    receipt["details"][field] = value
    with pytest.raises(JudgmentTransportError):
        verify_judgment_response(
            _response(record, status="committed", receipt_json=_encoded_receipt(receipt)),
            record.scope,
            record.payload_digest,
        )


@pytest.mark.parametrize("kind", ["duplicate", "whitespace", "oversize"])
def test_receipt_encoding_is_exact(home, kind):
    record = _prepared()
    receipt = _encoded_receipt(_receipt(record))
    if kind == "duplicate":
        receipt = receipt.replace(
            '"result":"committed"', '"result":"committed","result":"committed"'
        )
    elif kind == "whitespace":
        receipt += " "
    else:
        receipt += " " * 8192
    with pytest.raises(JudgmentTransportError):
        verify_judgment_response(
            _response(record, status="committed", receipt_json=receipt),
            record.scope,
            record.payload_digest,
        )


def test_wrong_account_cannot_send(home):
    from nauro.store.submission_records import SubmissionActorMismatchError

    record = _prepared()
    (home / "config.json").write_text(
        json.dumps({"auth": {"user_id": OTHER, "access_token": "other-token"}})
    )
    requests = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        transport = HttpJudgmentTransport("https://example.test", client)
        with pytest.raises(SubmissionActorMismatchError):
            transport.submit(record)
    assert requests == []


def test_nonobject_jwt_payload_is_rejected():
    import base64

    from nauro.auth import decode_jwt_payload

    body = base64.urlsafe_b64encode(b"[]").decode().rstrip("=")
    with pytest.raises(ValueError, match="JWT payload must be an object"):
        decode_jwt_payload(f"header.{body}.signature")


def test_account_switch_during_request_leaves_result_unresolved(home):
    from nauro.store.submission_records import SubmissionActorMismatchError

    record = _prepared()
    original = (home / "config.json").read_bytes()

    def handle(request):
        assert request.headers["Authorization"] == "Bearer synthetic-test-token"
        (home / "config.json").write_text(
            json.dumps({"auth": {"user_id": OTHER, "access_token": "other-token"}})
        )
        return httpx.Response(200, content=_response(record, status="pending"))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = HttpJudgmentTransport("https://example.test", client)
        with pytest.raises(SubmissionActorMismatchError):
            submit_judgment(record.scope, transport)
    (home / "config.json").write_bytes(original)
    assert read_submission(record.scope).phase == "uncertain"
