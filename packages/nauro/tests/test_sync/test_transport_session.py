from __future__ import annotations

import hashlib
import ssl
import traceback
from unittest.mock import MagicMock

import httpx
import pytest

from tests.conftest import seed_auth_config


def _response(status: int = 200, *, content: bytes = b"", etag: str = '"etag"'):
    return httpx.Response(status, content=content, headers={"ETag": etag})


def test_operation_session_reuses_and_closes_one_client(monkeypatch):
    from nauro.sync.remote import TransferSession, fetch_via_presigned_url

    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [_response(content=b"one"), _response(content=b"two")]
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: client)

    with TransferSession() as session:
        first = fetch_via_presigned_url("https://objects.example/one?secret=one", session=session)
        second = fetch_via_presigned_url("https://objects.example/two?secret=two", session=session)

    assert first.body == b"one"
    assert second.body == b"two"
    assert client.get.call_count == 2
    client.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Objects.Example/path?secret=value", "https://objects.example:443"),
        ("http://API.Example:8080/path", "http://api.example:8080"),
    ],
)
def test_canonical_origin_uses_lowercase_host_and_effective_port(url, expected):
    from nauro.sync.remote import canonical_origin

    assert canonical_origin(url) == expected


def test_one_session_reuses_the_client_across_api_and_object_calls(monkeypatch):
    from nauro.sync.remote import (
        TransferSession,
        fetch_manifest,
        fetch_via_presigned_url,
        request_presigned_urls,
    )

    seed_auth_config(variant="sync")
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [
        httpx.Response(200, json={"files": [], "next_cursor": None}),
        _response(content=b"body"),
    ]
    client.post.return_value = httpx.Response(
        200,
        json={
            "urls": [
                {
                    "verb": "GET",
                    "path": "stack.md",
                    "url": "https://objects.test/stack?signature=secret",
                }
            ]
        },
    )
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: client)

    with TransferSession() as session:
        assert fetch_manifest("project", session=session) == []
        urls = request_presigned_urls(
            "project", [{"verb": "GET", "path": "stack.md"}], session=session
        )
        fetched = fetch_via_presigned_url(urls[0]["url"], session=session)

    assert fetched.body == b"body"
    assert client.get.call_count == 2
    assert client.post.call_count == 1
    client.close.assert_called_once_with()


def test_invalid_api_response_keeps_its_http_status_without_rendering_body(monkeypatch):
    from nauro.sync.remote import TransferBoundaryError, TransferSession, fetch_manifest

    seed_auth_config(variant="sync")
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = httpx.Response(200, content=b"secret response body")
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: client)

    with TransferSession() as session:
        with pytest.raises(TransferBoundaryError) as caught:
            fetch_manifest("project", session=session)

    assert caught.value.status == 200
    assert caught.value.kind.value == "invalid-response"
    assert "secret response body" not in str(caught.value)


def test_put_accepts_any_success_status_and_marks_a_missing_etag_unverified():
    from nauro.sync.remote import TransferSession, WriteOutcome, put_via_presigned_url

    client = MagicMock(spec=httpx.Client)
    client.put.return_value = httpx.Response(201)

    with TransferSession(client=client) as session:
        result = put_via_presigned_url(
            "https://objects.test/stack?signature=secret",
            b"body",
            session=session,
        )

    assert result.outcome is WriteOutcome.WRITTEN_UNVERIFIED


def test_certificate_failure_trips_only_its_origin_and_sanitizes_error(monkeypatch):
    from nauro.sync.remote import TransferBoundaryError, TransferSession, fetch_via_presigned_url

    certificate_error = ssl.SSLCertVerificationError("certificate rejected secret=leaked")
    nested = httpx.ConnectError("connect failed", request=httpx.Request("GET", "https://a.test"))
    nested.__cause__ = certificate_error

    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [nested, _response(content=b"other")]
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: client)
    first_url = "https://a.test/object?X-Amz-Signature=secret"
    second_url = "https://a.test/other?X-Amz-Signature=other"
    other_url = "https://b.test/object?token=still-secret"

    with TransferSession() as session:
        with pytest.raises(TransferBoundaryError) as first:
            fetch_via_presigned_url(first_url, session=session)
        with pytest.raises(TransferBoundaryError) as second:
            fetch_via_presigned_url(second_url, session=session)
        fetched = fetch_via_presigned_url(other_url, session=session)

    assert first.value.origin == "https://a.test:443"
    assert first.value.kind.value == "tls-certificate"
    assert first.value.__cause__ is None
    assert first.value.__context__ is None
    assert second.value.kind.value == "origin-aborted"
    assert fetched.body == b"other"
    assert client.get.call_count == 2
    rendered = f"{first.value!r} {first.value} {second.value!r} {second.value}"
    rendered += "".join(
        traceback.format_exception(type(first.value), first.value, first.value.__traceback__)
    )
    assert "X-Amz" not in rendered
    assert "secret" not in rendered


def test_permanent_manifest_http_failure_blocks_same_origin_push_but_not_other_origin(
    tmp_path, monkeypatch
):
    from nauro.sync.pull import run_pull
    from nauro.sync.push import push_changed_files
    from nauro.sync.remote import TransferSession, fetch_via_presigned_url
    from nauro.sync.transfer import NullReporter

    seed_auth_config(variant="sync")
    monkeypatch.setenv("NAURO_API_URL", "https://api.test")
    store = tmp_path / "store"
    store.mkdir()
    (store / "stack.md").write_bytes(b"local")
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [
        httpx.Response(403),
        _response(content=b"other"),
    ]
    client.post.return_value = httpx.Response(
        200,
        json={
            "urls": [
                {
                    "verb": "PUT",
                    "path": "stack.md",
                    "url": "https://api.test/stack",
                }
            ]
        },
    )
    client.put.return_value = _response()

    with TransferSession(client=client) as session:
        pull_report = run_pull("project", store, NullReporter(), session=session)
        push_report = push_changed_files("project", store, session=session)
        other = fetch_via_presigned_url("https://objects.test/other", session=session)

    assert pull_report.origin_aborted == ("https://api.test:443",)
    assert push_report.origin_aborted == ("stack.md",)
    client.post.assert_not_called()
    client.put.assert_not_called()
    assert other.body == b"other"


def test_object_404_does_not_trip_the_origin_for_another_object():
    from nauro.sync.remote import TransferBoundaryError, TransferSession, fetch_via_presigned_url

    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [
        httpx.Response(404),
        _response(content=b"present"),
    ]

    with TransferSession(client=client) as session:
        with pytest.raises(TransferBoundaryError) as caught:
            fetch_via_presigned_url("https://objects.test/missing", session=session)
        fetched = fetch_via_presigned_url("https://objects.test/present", session=session)

    assert caught.value.status == 404
    assert not caught.value.aborts_origin
    assert fetched.body == b"present"
    assert client.get.call_count == 2


@pytest.mark.parametrize(
    ("error", "retry", "put_outcome"),
    [
        (httpx.PoolTimeout("pool"), "transient", "not-written"),
        (httpx.WriteTimeout("write"), "transient", "ambiguous"),
        (httpx.ReadError("read"), "transient", "ambiguous"),
        (httpx.RemoteProtocolError("protocol"), "transient", "ambiguous"),
        (RuntimeError("unknown"), "permanent", "ambiguous"),
    ],
)
def test_retry_and_put_outcome_classification_are_independent(error, retry, put_outcome):
    from nauro.sync.remote import classify_exception

    classification = classify_exception(error, operation="PUT")

    assert classification.retry.value == retry
    assert classification.write_outcome.value == put_outcome


def test_only_presigned_transfer_operations_treat_403_as_an_expiry_candidate():
    from nauro.sync.remote import TransferOperation, classify_status

    assert classify_status(403, operation=TransferOperation.GET).retry.value == "expired-candidate"
    assert classify_status(403, operation=TransferOperation.PUT).retry.value == "expired-candidate"
    assert classify_status(403, operation=TransferOperation.MANIFEST).retry.value == "permanent"
    assert classify_status(403, operation=TransferOperation.PRESIGN).retry.value == "permanent"


def test_push_binds_state_to_the_exact_payload_and_leaves_a_later_edit_pending(
    tmp_path, monkeypatch
):
    from nauro.sync.push import plan_push, push_changed_files
    from nauro.sync.remote import PutResult, WriteOutcome
    from nauro.sync.state import load_state

    store = tmp_path / "store"
    store.mkdir()
    target = store / "stack.md"
    target.write_bytes(b"sent")
    seen: list[bytes] = []

    monkeypatch.setattr(
        "nauro.sync.remote.request_presigned_urls",
        lambda *_args, **_kwargs: [
            {"verb": "PUT", "path": "stack.md", "url": "https://objects.test/stack"}
        ],
    )

    def put(_url: str, payload: bytes, **_kwargs):
        seen.append(payload)
        target.write_bytes(b"later")
        return PutResult(etag='"sent"', outcome=WriteOutcome.VERIFIED)

    monkeypatch.setattr("nauro.sync.remote.put_via_presigned_url", put)

    report = push_changed_files("project", store)

    assert report.verified == ("stack.md",)
    assert seen == [b"sent"]
    assert load_state(store).files["stack.md"].local_sha256 == hashlib.sha256(b"sent").hexdigest()

    next_plan = plan_push(store, load_state(store))
    assert tuple(item.relative_path for item in next_plan.candidates) == ("stack.md",)


def test_push_report_is_complete_only_when_every_planned_item_is_verified():
    from nauro.sync.push import PushReport

    assert not PushReport(planned=("stack.md",)).is_complete
    assert not PushReport(planned=("stack.md",), verified=("other.md",)).is_complete
    assert PushReport(planned=("stack.md",), verified=("stack.md",)).is_complete


def test_pre_send_put_fault_retries_the_frozen_payload_within_the_budget(monkeypatch):
    from nauro.sync.push import _put_frozen
    from nauro.sync.remote import (
        PutResult,
        TransferBoundaryError,
        TransferSession,
        WriteOutcome,
        classify_exception,
    )

    classification = classify_exception(httpx.ConnectTimeout("connect"), operation="PUT")
    error = TransferBoundaryError(
        operation="PUT",
        origin="https://objects.test:443",
        kind=classification.kind,
        retry=classification.retry,
        write_outcome=classification.write_outcome,
    )
    put = MagicMock(
        side_effect=[
            error,
            error,
            PutResult(etag='"etag"', outcome=WriteOutcome.VERIFIED),
        ]
    )
    waits: list[float] = []
    monkeypatch.setattr("nauro.sync.remote.put_via_presigned_url", put)
    monkeypatch.setattr("nauro.sync.push.pause", waits.append)
    monkeypatch.setattr("nauro.sync.push.backoff_delay", lambda failures: float(failures))

    with TransferSession(client=MagicMock(spec=httpx.Client)) as session:
        result = _put_frozen(
            "project",
            "stack.md",
            "https://objects.test/stack",
            b"frozen",
            session,
        )

    assert result.outcome is WriteOutcome.VERIFIED
    assert [call.args[1] for call in put.call_args_list] == [b"frozen"] * 3
    assert waits == [1.0, 2.0]


def test_put_403_remints_once_before_retrying(monkeypatch):
    from nauro.sync.push import _put_frozen
    from nauro.sync.remote import (
        PutResult,
        TransferBoundaryError,
        TransferSession,
        WriteOutcome,
        classify_status,
    )

    classification = classify_status(403, operation="PUT")
    error = TransferBoundaryError(
        operation="PUT",
        origin="https://objects.test:443",
        status=403,
        kind=classification.kind,
        retry=classification.retry,
        write_outcome=classification.write_outcome,
    )
    put = MagicMock(side_effect=[error, PutResult(etag='"etag"', outcome=WriteOutcome.VERIFIED)])
    presign = MagicMock(
        return_value=[
            {
                "verb": "PUT",
                "path": "stack.md",
                "url": "https://objects.test/replacement",
            }
        ]
    )
    monkeypatch.setattr("nauro.sync.remote.put_via_presigned_url", put)
    monkeypatch.setattr("nauro.sync.remote.request_presigned_urls", presign)

    with TransferSession(client=MagicMock(spec=httpx.Client)) as session:
        result = _put_frozen(
            "project",
            "stack.md",
            "https://objects.test/original",
            b"frozen",
            session,
        )

    assert result.outcome is WriteOutcome.VERIFIED
    assert [call.args[0] for call in put.call_args_list] == [
        "https://objects.test/original",
        "https://objects.test/replacement",
    ]
    presign.assert_called_once()


def test_unverified_put_reconciles_from_one_get_response_and_ignores_manifest_etag(
    tmp_path, monkeypatch
):
    from nauro.sync.push import push_changed_files
    from nauro.sync.remote import FetchedObject, PutResult, WriteOutcome
    from nauro.sync.state import load_state

    store = tmp_path / "store"
    store.mkdir()
    (store / "stack.md").write_bytes(b"sent")
    presigns = iter(
        [
            [{"verb": "PUT", "path": "stack.md", "url": "https://objects.test/put"}],
            [{"verb": "GET", "path": "stack.md", "url": "https://objects.test/get"}],
        ]
    )
    monkeypatch.setattr(
        "nauro.sync.remote.request_presigned_urls", lambda *_args, **_kwargs: next(presigns)
    )
    monkeypatch.setattr(
        "nauro.sync.remote.put_via_presigned_url",
        lambda *_args, **_kwargs: PutResult(etag="", outcome=WriteOutcome.WRITTEN_UNVERIFIED),
    )
    monkeypatch.setattr(
        "nauro.sync.remote.fetch_manifest",
        lambda *_args, **_kwargs: [{"path": "stack.md", "etag": '"manifest"'}],
    )
    monkeypatch.setattr(
        "nauro.sync.remote.fetch_via_presigned_url",
        lambda *_args, **_kwargs: FetchedObject(body=b"sent", etag='"response"'),
    )

    report = push_changed_files("project", store)

    assert report.verified == ("stack.md",)
    assert load_state(store).files["stack.md"].remote_etag == '"response"'


def test_divergent_reconciliation_fails_without_advancing_state(tmp_path, monkeypatch):
    from nauro.sync.push import plan_push, push_changed_files
    from nauro.sync.remote import FetchedObject, PutResult, WriteOutcome
    from nauro.sync.state import load_state

    store = tmp_path / "store"
    store.mkdir()
    (store / "stack.md").write_bytes(b"sent")
    presigns = iter(
        [
            [{"verb": "PUT", "path": "stack.md", "url": "https://objects.test/put"}],
            [{"verb": "GET", "path": "stack.md", "url": "https://objects.test/get"}],
        ]
    )
    monkeypatch.setattr(
        "nauro.sync.remote.request_presigned_urls", lambda *_args, **_kwargs: next(presigns)
    )
    monkeypatch.setattr(
        "nauro.sync.remote.put_via_presigned_url",
        lambda *_args, **_kwargs: PutResult(etag="", outcome=WriteOutcome.WRITTEN_UNVERIFIED),
    )
    monkeypatch.setattr(
        "nauro.sync.remote.fetch_manifest",
        lambda *_args, **_kwargs: [{"path": "stack.md", "etag": '"manifest"'}],
    )
    monkeypatch.setattr(
        "nauro.sync.remote.fetch_via_presigned_url",
        lambda *_args, **_kwargs: FetchedObject(body=b"different", etag='"response"'),
    )

    report = push_changed_files("project", store)
    state = load_state(store)

    assert report.failed == ("stack.md",)
    assert "stack.md" not in state.files
    assert tuple(item.relative_path for item in plan_push(store, state).candidates) == ("stack.md",)


@pytest.mark.parametrize(
    "source_error",
    [
        httpx.WriteTimeout("write"),
        httpx.ReadTimeout("read"),
        httpx.WriteError("write"),
        httpx.ReadError("read"),
        httpx.RemoteProtocolError("protocol"),
        RuntimeError("unknown"),
    ],
)
def test_ambiguous_put_faults_are_observed_without_a_blind_retry(
    source_error, tmp_path, monkeypatch
):
    from nauro.sync.push import push_changed_files
    from nauro.sync.remote import (
        TransferBoundaryError,
        classify_exception,
    )

    store = tmp_path / "store"
    store.mkdir()
    (store / "stack.md").write_bytes(b"sent")
    monkeypatch.setattr(
        "nauro.sync.remote.request_presigned_urls",
        lambda *_args, **_kwargs: [
            {"verb": "PUT", "path": "stack.md", "url": "https://objects.test/stack"}
        ],
    )
    classification = classify_exception(source_error, operation="PUT")
    error = TransferBoundaryError(
        operation="PUT",
        origin="https://objects.test:443",
        kind=classification.kind,
        retry=classification.retry,
        write_outcome=classification.write_outcome,
    )
    calls = 0

    def put(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr("nauro.sync.remote.put_via_presigned_url", put)
    monkeypatch.setattr("nauro.sync.remote.fetch_manifest", lambda *_args, **_kwargs: [])

    report = push_changed_files("project", store)

    assert calls == 1
    assert report.ambiguous == ("stack.md",)


def test_http_5xx_put_is_ambiguous_and_is_not_blindly_retried(tmp_path, monkeypatch):
    from nauro.sync.push import push_changed_files
    from nauro.sync.remote import TransferBoundaryError, classify_status

    store = tmp_path / "store"
    store.mkdir()
    (store / "stack.md").write_bytes(b"sent")
    monkeypatch.setattr(
        "nauro.sync.remote.request_presigned_urls",
        lambda *_args, **_kwargs: [
            {"verb": "PUT", "path": "stack.md", "url": "https://objects.test/stack"}
        ],
    )
    classification = classify_status(503, operation="PUT")
    error = TransferBoundaryError(
        operation="PUT",
        origin="https://objects.test:443",
        status=503,
        kind=classification.kind,
        retry=classification.retry,
        write_outcome=classification.write_outcome,
    )
    put = MagicMock(side_effect=error)
    monkeypatch.setattr("nauro.sync.remote.put_via_presigned_url", put)
    monkeypatch.setattr("nauro.sync.remote.fetch_manifest", lambda *_args, **_kwargs: [])

    report = push_changed_files("project", store)

    put.assert_called_once()
    assert report.ambiguous == ("stack.md",)


@pytest.mark.parametrize(
    ("case", "expected", "etag"),
    [
        ("matching", "verified", '"response"'),
        ("missing-etag", "pending", ""),
        ("divergent", "divergent", ""),
        ("absent", "pending", ""),
        ("failed", "pending", ""),
    ],
)
def test_reconciliation_outcomes_use_one_get_response(case, expected, etag, monkeypatch):
    from nauro.sync.push import _reconcile
    from nauro.sync.remote import FetchedObject, PresignError, TransferSession

    payload_sha = hashlib.sha256(b"sent").hexdigest()
    monkeypatch.setattr(
        "nauro.sync.remote.fetch_manifest",
        lambda *_args, **_kwargs: (
            [] if case == "absent" else [{"path": "stack.md", "etag": '"manifest"'}]
        ),
    )
    monkeypatch.setattr(
        "nauro.sync.remote.request_presigned_urls",
        lambda *_args, **_kwargs: [
            {"verb": "GET", "path": "stack.md", "url": "https://objects.test/get"}
        ],
    )

    def fetch(*_args, **_kwargs):
        if case == "failed":
            raise PresignError("observation failed")
        body = b"different" if case == "divergent" else b"sent"
        response_etag = "" if case == "missing-etag" else '"response"'
        return FetchedObject(body, response_etag)

    monkeypatch.setattr("nauro.sync.remote.fetch_via_presigned_url", fetch)

    with TransferSession(client=MagicMock(spec=httpx.Client)) as session:
        outcome = _reconcile("project", "stack.md", payload_sha, session)

    assert outcome.kind.value == expected
    assert outcome.etag == etag


def test_checkpoint_failure_stops_the_batch_and_preserves_prior_state(tmp_path, monkeypatch):
    from nauro.sync import state as state_module
    from nauro.sync.push import push_changed_files
    from nauro.sync.remote import PutResult, WriteOutcome

    store = tmp_path / "store"
    store.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (store / name).write_text(name)
    monkeypatch.setattr(
        "nauro.sync.remote.request_presigned_urls",
        lambda _project, operations, **_kwargs: [
            {
                "verb": "PUT",
                "path": operation["path"],
                "url": f"https://objects.test/{operation['path']}",
            }
            for operation in operations
        ],
    )
    put = MagicMock(
        side_effect=lambda url, *_args, **_kwargs: PutResult(
            etag=f'"{url.rsplit("/", 1)[1]}"', outcome=WriteOutcome.VERIFIED
        )
    )
    monkeypatch.setattr("nauro.sync.remote.put_via_presigned_url", put)
    real_save = state_module.save_state
    saves = 0

    def save(store_path, state):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("disk full")
        real_save(store_path, state)

    monkeypatch.setattr(state_module, "save_state", save)

    report = push_changed_files("project", store)

    assert report.verified == report.planned[:1]
    assert report.failed == report.planned[1:]
    assert put.call_count == 2
    persisted = state_module.load_state(store)
    assert tuple(persisted.files) == report.verified
