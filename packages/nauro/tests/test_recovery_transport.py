import json

import httpx
import pytest
from nauro_core.operations.commit_plan import canonical_judgment_payload_bytes

from nauro.auth import ActiveCredentials
from nauro.store.recovery_actions import timestamp
from nauro.sync import recovery_transport as transport
from tests.test_recovery_actions import ACTOR, CREATED, PROJECT, SAGA, action


def lookup(saved=None, status="absent"):
    saved = saved or action()
    receipt = canonical_judgment_payload_bytes(
        {
            "receipt_id": SAGA,
            "operation_kind": "judgment_recovery",
            "operation_id": saved.action_id,
            "result": "resumed",
            "committed_at": CREATED,
            "target_kind": "judgment_saga",
            "target_id": SAGA,
            "details": {
                "disposition": "resume",
                "source_fencing_token": 2,
                "resulting_fencing_token": 3,
            },
        }
    ).decode()
    return {
        "kind": "action",
        "scope": {
            "project_id": PROJECT,
            "user_id": ACTOR,
            "operation_kind": "judgment_recovery",
            "operation_id": saved.action_id,
        },
        "payload_digest": saved.payload_digest if status == "accepted" else None,
        "receipt_json": receipt if status == "accepted" else None,
        "status": status,
        "judgment": None,
    }


def connect(handler, credentials=lambda: ActiveCredentials(ACTOR, "synthetic")):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return transport.RecoveryTransport(
        "https://probe.example/mcp",
        PROJECT,
        ACTOR,
        client,
        credentials,
        clock=lambda: timestamp(CREATED),
    )


def response(request, result):
    return httpx.Response(
        200, json={"version": 1, "mode": json.loads(request.content)["mode"], "result": result}
    )


def test_absent_and_accepted_lookup_never_dispatch():
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return response(request, lookup(status="absent" if len(calls) == 1 else "accepted"))

    client = connect(handle)
    assert client.lookup(action().action_id)["status"] == "absent"
    assert client.lookup(action().action_id, action())["status"] == "accepted"
    assert [call["mode"] for call in calls] == ["lookup", "lookup"]


@pytest.mark.parametrize("status", [301, 307, 401, 403, 409, 429, 500, 503])
def test_failure_never_retries_or_redirects(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example"})

    with pytest.raises(ValueError):
        connect(handle).dispatch(action())
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer synthetic"
    assert json.loads(calls[0].content)["action_payload"] == action().action_payload


def test_expiry_after_response_refuses_evidence():
    calls = []

    def credentials():
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("expired")
        return ActiveCredentials(ACTOR, "synthetic")

    client = connect(lambda request: response(request, lookup()), credentials)
    with pytest.raises(ValueError, match="expired"):
        client.lookup(action().action_id)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "field,value", [("status", "unresolved"), ("payload_digest", "f" * 64), ("receipt_json", "{}")]
)
def test_unbound_lookup_refuses(field, value):
    result = {**lookup(status="accepted"), field: value}
    with pytest.raises(ValueError):
        connect(lambda request: response(request, result)).lookup(action().action_id, action())


@pytest.mark.parametrize(
    "field,value", [("operation_id", "wrong"), ("target_id", PROJECT), ("result", "abandoned")]
)
def test_receipt_must_bind_exact_action(field, value):
    result = lookup(status="accepted")
    receipt = json.loads(result["receipt_json"])
    receipt[field] = value
    result["receipt_json"] = canonical_judgment_payload_bytes(receipt).decode()
    with pytest.raises(ValueError):
        connect(lambda request: response(request, result)).lookup(action().action_id, action())


@pytest.mark.parametrize("raw", [b'{"version":1,"version":1}', b"[]", b"null", b"NaN"])
def test_malformed_envelope_refuses(raw):
    with pytest.raises((ValueError, TypeError)):
        connect(lambda _: httpx.Response(200, content=raw)).lookup("saved")


def test_response_limit_refuses(monkeypatch):
    monkeypatch.setattr(transport, "MAX_RECOVERY_RESPONSE", 32)
    with pytest.raises(ValueError, match="size limit"):
        connect(lambda _: httpx.Response(200, content=b"x" * 33)).lookup("saved")


@pytest.mark.parametrize("cursor,after", [("wrong", None), ("recovery:test", "recovery:test")])
def test_discovery_cursor_refuses(cursor, after):
    page = {"kind": "page", "actions": [lookup(status="accepted")], "next_after": cursor}
    with pytest.raises(ValueError):
        connect(lambda request: response(request, page)).discover(after)


def test_lost_dispatch_does_not_resend():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("response lost")

    with pytest.raises(ValueError, match="unknown"):
        connect(handle).dispatch(action())
    assert len(calls) == 1


def test_deadline_crossed_during_credentials_prevents_transmission():
    from tests.test_recovery_actions import DEADLINE

    now = timestamp(CREATED)

    def credentials():
        nonlocal now
        now = timestamp(DEADLINE)
        return ActiveCredentials(ACTOR, "synthetic")

    client = connect(lambda _: pytest.fail("expired transmission"), credentials)
    client.clock = lambda: now
    with pytest.raises(ValueError, match="admission window"):
        client.dispatch(action())
