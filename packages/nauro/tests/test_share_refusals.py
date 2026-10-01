"""Closed HTTP refusals preserve evidence about the original saved write."""

import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.mcp import share_responses
from nauro.store import share_records as records
from nauro.store.share_contract import ShareRefused, ShareTransportError
from nauro.sync import share_submission as submission
from nauro.sync.share_transport import HttpShareTransport
from tests.test_share_submission import _body, _prepared, _result, home

__all__ = ["home"]


@pytest.mark.parametrize("status,code", [(403, "actor_mismatch"), (409, "single_writer_refused")])
def test_first_submit_refusal_is_durable_and_terminal(home, status, code):
    record = _prepared()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": code})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpShareTransport("https://example.test", client)
        result = submission.submit_share(record.scope, transport)
        assert result.status == "refused"
        assert result.server_code == code
        assert result.http_status == status
        assert result.request_mode == "submit"
        assert result.unresolved is False
        saved = records.read_share_submission(record.scope)
        assert saved.result == result
        assert saved.phase == "resolved"
        assert saved.scope == record.scope
        assert saved.payload_digest == record.payload_digest
        for mode in (
            submission.submit_share,
            submission.retry_share,
            submission.recover_share,
        ):
            assert mode(record.scope, transport) == result
    assert calls == ["/share/submit"]


@pytest.mark.parametrize("status,code", [(403, "actor_mismatch"), (409, "single_writer_refused")])
@pytest.mark.parametrize("mode", [submission.recover_share, submission.retry_share])
def test_lookup_refusal_retains_uncertainty_until_authority_returns(home, status, code, mode):
    record = _prepared()
    with pytest.raises(ShareTransportError):
        submission.submit_share(record.scope, Mock(submit=Mock(side_effect=ShareTransportError())))
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": code})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = mode(record.scope, HttpShareTransport("https://example.test", client))
    assert result.status == "refused"
    assert result.server_code == code
    assert result.http_status == status
    assert result.request_mode == "lookup"
    assert result.unresolved is True
    saved = records.read_share_submission(record.scope)
    assert saved.phase == "uncertain"
    assert saved.result == result
    assert calls == ["/share/lookup"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_body(record)))
    ) as client:
        recovered = submission.recover_share(
            record.scope, HttpShareTransport("https://example.test", client)
        )
    assert recovered.status == "committed"
    assert records.read_share_submission(record.scope).phase == "resolved"


@pytest.mark.parametrize("status,code", [(403, "forbidden"), (400, "invalid_request")])
def test_refusal_that_can_follow_publication_remains_uncertain(home, status, code):
    record = _prepared()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": code}))
    ) as client:
        result = submission.submit_share(
            record.scope, HttpShareTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_share_submission(record.scope).phase == "uncertain"


def test_retry_refusal_does_not_erase_original_unknown_send(home):
    record = _prepared()
    records.mark_share_uncertain(record)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("lookup"):
            return httpx.Response(200, json=_body(record, "absent"))
        return httpx.Response(409, json={"detail": "single_writer_refused"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = submission.retry_share(
            record.scope, HttpShareTransport("https://example.test", client)
        )
    assert result.status == "refused"
    assert result.unresolved is True
    assert records.read_share_submission(record.scope).phase == "uncertain"
    assert calls == ["/share/lookup", "/share/submit"]


@pytest.mark.parametrize("route", ["submit", "lookup"])
@pytest.mark.parametrize(
    "status,raw",
    [
        (403, b'{"detail":"unknown_refusal"}'),
        (409, b'{"detail":"actor_mismatch"}'),
        (403, b'{"detail":"forbidden","extra":true}'),
        (403, b'{"detail":"forbidden","detail":"actor_mismatch"}'),
        (403, b'{"detail":null}'),
        (403, b"not json"),
        (503, b'{"detail":"single_writer_refused"}'),
        (429, b'{"detail":"rate_limited"}'),
        (302, b'{"detail":"actor_mismatch"}'),
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
        pytest.raises(ShareTransportError),
    ):
        getattr(submission, "submit_share" if route == "submit" else "recover_share")(
            record.scope, HttpShareTransport("https://example.test", client)
        )
    assert calls == [f"/share/{route}"]
    assert records.read_share_submission(record.scope).phase == (
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
        pytest.raises(ShareTransportError),
    ):
        submission.submit_share(record.scope, HttpShareTransport("https://example.test", client))
    assert records.read_share_submission(record.scope).phase == "uncertain"


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
def test_dormant_response_adapter_preserves_refusal_evidence(home, mode):
    record = _prepared()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"detail": "actor_mismatch"})
        )
    ) as client:
        response = getattr(share_responses, f"{mode}_share")(
            record.scope, HttpShareTransport("https://example.test", client)
        )
    assert response.isError is True
    assert response.structuredContent["status"] == "refused"
    assert response.structuredContent["server_code"] == "actor_mismatch"
    assert response.structuredContent["unresolved"] is (mode != "submit")
    assert response.content[0].text == "Share request refused: actor_mismatch. " + (
        "This attempt did not write. Correct the refusal before a new attempt."
        if mode == "submit"
        else (
            "The original write outcome remains unknown. Restore access and recover this reference."
        )
    )


@pytest.mark.parametrize("route", ["submit", "lookup"])
@pytest.mark.parametrize("status", [200, 403])
def test_all_http_bodies_obey_response_byte_limit(home, route, status):
    record = _prepared()
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, content=b" " * (64 * 1024 + 1))
            )
        ) as client,
        pytest.raises(ShareTransportError, match="byte limit"),
    ):
        getattr(HttpShareTransport("https://example.test", client), route)(record)


@pytest.mark.parametrize("mode", ["submit", "retry"])
def test_custom_transport_cannot_supply_terminal_refusal(home, mode):
    record = _prepared()
    if mode == "retry":
        records.mark_share_uncertain(record)
    refusal = ShareRefused(
        version=1,
        scope=record.scope,
        payload_digest=record.payload_digest,
        http_status=403,
        server_code="actor_mismatch",
        request_mode="submit",
        unresolved=False,
    )
    transport = Mock(
        lookup=Mock(return_value=_result(record, "absent")), submit=Mock(return_value=refusal)
    )
    with pytest.raises(ShareTransportError, match="terminal refusal"):
        getattr(submission, f"{mode}_share")(record.scope, transport)
    saved = records.read_share_submission(record.scope)
    assert saved.phase == "uncertain"
    assert saved.result == (_result(record, "absent") if mode == "retry" else None)
