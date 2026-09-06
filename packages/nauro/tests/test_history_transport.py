from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.store.generation_projection import GenerationProjectionTarget
from nauro.sync import history_transport as transport
from tests.test_generation_installation import USER_ID, _projection

OTHER = "01K44444444444444444444444"


def response_body(target, days=None):
    identity = target.identity
    baseline = {"generation_id": OTHER, "committed_at": "2020-01-01T00:00:00.000000Z"}
    diff = "  - Removed file: decisions/001-deleted.md"
    return {
        "version": 1,
        "store": "remote",
        "authenticated_user_id": USER_ID,
        "projection_class": identity.projection_class,
        "projection_scope_id": identity.projection_scope_id,
        "read_authority": {
            "kind": "generation",
            "project_id": identity.project_id,
            "generation_id": identity.generation_id,
            "manifest_digest": identity.manifest_digest,
            "committed_at": identity.committed_at,
            "freshness": "authorized_at_read",
        },
        "baseline": baseline,
        "selection": "latest_two" if days is None else "cutoff",
        "requested_days": days,
        "cutoff_date_used": None if days is None else "2026-09-01T00:00:00+00:00",
        "diff": diff,
        "text": diff + f"\n\nBaseline: {OTHER}. Committed: {baseline['committed_at']}.\n"
        f"Generation: {identity.generation_id}. Committed: {identity.committed_at}.\n"
        "Authorization checked for this read.",
    }


@pytest.fixture
def target(monkeypatch):
    monkeypatch.setattr(
        transport, "read_active_credentials", lambda: ActiveCredentials(USER_ID, "synthetic-token")
    )
    return _projection().target


def verify(body, target, days=None):
    return transport.verify_history_response(json.dumps(body).encode(), target, days)


@pytest.mark.parametrize("days", [None, 0, -1, 7])
def test_exact_request_and_bound_response(target, days):
    requests = []
    body = response_body(target, days)

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = transport.HttpHistoryTransport(target.binding.server_url, client).fetch(
            target, days
        )
    assert result.model_dump() == body
    assert len(requests) == 1
    assert str(requests[0].url) == "https://mcp.nauro.ai/generations/history"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-token"
    identity = target.identity
    assert json.loads(requests[0].content) == {
        "version": 1,
        "project_id": identity.project_id,
        "expected_user_id": USER_ID,
        "expected_generation_id": identity.generation_id,
        "expected_manifest_digest": identity.manifest_digest,
        "expected_projection_scope_id": identity.projection_scope_id,
        "days": days,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("store", "local"),
        ("authenticated_user_id", OTHER),
        ("projection_class", "viewer"),
        ("projection_scope_id", "b" * 64),
        ("requested_days", 1),
        ("requested_days", True),
        ("selection", "approximate"),
        ("baseline", None),
        ("cutoff_date_used", "invalid"),
        ("archive", {}),
        ("text", "forged frame"),
        pytest.param("diff", "x" * 12001, id="oversized-diff"),
    ],
)
def test_malformed_or_unbound_response_is_refused(target, field, value):
    body = response_body(target)
    body[field] = value
    with pytest.raises(transport.HistoryTransportError):
        verify(body, target)


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", OTHER),
        ("generation_id", OTHER),
        ("manifest_digest", "b" * 64),
        ("committed_at", "2020-01-01T00:00:00.000000Z"),
        ("freshness", "cached"),
        ("kind", "legacy"),
    ],
)
def test_authority_facts_must_match(target, field, value):
    body = response_body(target)
    body["read_authority"][field] = value
    with pytest.raises(transport.HistoryTransportError):
        verify(body, target)


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "store",
        "authenticated_user_id",
        "projection_class",
        "projection_scope_id",
        "read_authority",
        "baseline",
        "selection",
        "requested_days",
        "cutoff_date_used",
        "diff",
        "text",
    ],
)
def test_all_response_fields_are_required(target, field):
    body = response_body(target)
    del body[field]
    with pytest.raises(transport.HistoryTransportError):
        verify(body, target)


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b"NaN",
        b"\xff",
        b'{"version":1,"version":1}',
        pytest.param(b"x" * 170001, id="oversized-response"),
    ],
)
def test_strict_bounded_json(target, raw):
    with pytest.raises(transport.HistoryTransportError):
        transport.verify_history_response(raw, target, None)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 409, 429, 500, 503])
def test_no_redirect_or_automatic_resend(target, status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example/history"})

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(transport.HistoryTransportError):
            transport.HttpHistoryTransport(target.binding.server_url, client).fetch(target)
    assert len(requests) == 1


@pytest.mark.parametrize("fault", ["read", "timeout", "overflow", "account"])
def test_failed_or_interrupted_response_never_escapes(target, monkeypatch, fault):
    calls = []

    def handle(request):
        calls.append(request)
        if fault == "read":
            raise httpx.ReadError("PRIVATE")
        if fault == "timeout":
            raise httpx.ReadTimeout("PRIVATE")
        if fault == "overflow":
            return httpx.Response(200, content=b"x" * 170001)
        monkeypatch.setattr(
            transport, "read_active_credentials", lambda: ActiveCredentials(OTHER, "other-token")
        )
        return httpx.Response(200, json=response_body(target))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(transport.HistoryTransportError) as caught:
            transport.HttpHistoryTransport(target.binding.server_url, client).fetch(target)
    assert "PRIVATE" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["actor", "binding", "days"])
def test_local_mismatch_precedes_any_request(target, monkeypatch, fault):
    if fault == "actor":
        monkeypatch.setattr(
            transport, "read_active_credentials", lambda: ActiveCredentials(OTHER, "other-token")
        )
    elif fault == "binding":
        target = GenerationProjectionTarget(
            replace(target.binding, server_url="https://other.example"), target.identity
        )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected request"))
    ) as client:
        with pytest.raises(transport.HistoryTransportError):
            transport.HttpHistoryTransport("https://mcp.nauro.ai", client).fetch(
                target, True if fault == "days" else None
            )


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.nauro.ai",
        "https://user:pass@mcp.nauro.ai",
        "https://mcp.nauro.ai/x",
        "https://mcp.nauro.ai?x=1",
        "https://mcp.nauro.ai#x",
    ],
)
def test_origin_is_exact_https(url):
    with httpx.Client() as client:
        with pytest.raises(transport.HistoryTransportError):
            transport.HttpHistoryTransport(url, client)


@pytest.mark.parametrize("days", [True, "1", 1.0])
def test_verifier_rejects_coerced_request_days(target, days):
    with pytest.raises(transport.HistoryTransportError):
        verify(response_body(target, 1), target, days)
