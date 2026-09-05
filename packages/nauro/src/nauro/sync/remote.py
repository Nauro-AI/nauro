"""Typed HTTP boundary for legacy manifest and presign sync."""

from __future__ import annotations

import os
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from nauro.auth import DEFAULT_API_URL, with_token_refresh
from nauro.store.config import load_config

_DEFAULT_API_TIMEOUT = 15.0
_DEFAULT_TRANSFER_TIMEOUT = 60.0
_CLIENT_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=10)
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_EXPIRED_STATUS = 403


class TransferOperation(str, Enum):
    MANIFEST = "manifest"
    PRESIGN = "presign"
    GET = "GET"
    PUT = "PUT"


class TransportFaultKind(str, Enum):
    INVALID_URL = "invalid-url"
    UNSUPPORTED_PROTOCOL = "unsupported-protocol"
    POOL_TIMEOUT = "pool-timeout"
    CONNECT_TIMEOUT = "connect-timeout"
    CONNECT_ERROR = "connect-error"
    TLS_CERTIFICATE = "tls-certificate"
    WRITE_TIMEOUT = "write-timeout"
    READ_TIMEOUT = "read-timeout"
    WRITE_ERROR = "write-error"
    READ_ERROR = "read-error"
    REMOTE_PROTOCOL = "remote-protocol-error"
    HTTP_STATUS = "http-status"
    INVALID_RESPONSE = "invalid-response"
    ORIGIN_ABORTED = "origin-aborted"
    UNKNOWN = "unknown"


class RetryClassification(str, Enum):
    TRANSIENT = "transient"
    EXPIRED_CANDIDATE = "expired-candidate"
    PERMANENT = "permanent"


class WriteOutcome(str, Enum):
    NOT_APPLICABLE = "not-applicable"
    NOT_WRITTEN = "not-written"
    AMBIGUOUS = "ambiguous"
    WRITTEN_UNVERIFIED = "written-unverified"
    VERIFIED = "verified"


@dataclass(frozen=True)
class FaultClassification:
    kind: TransportFaultKind
    retry: RetryClassification
    write_outcome: WriteOutcome


class PresignError(Exception):
    """Base class for the legacy sync HTTP boundary."""


class TransferBoundaryError(PresignError):
    """One sanitized manifest, presign, GET, or PUT boundary failure."""

    def __init__(
        self,
        *,
        operation: str | TransferOperation,
        origin: str,
        kind: TransportFaultKind,
        retry: RetryClassification,
        write_outcome: WriteOutcome,
        status: int | None = None,
    ) -> None:
        self.operation = TransferOperation(operation)
        self.origin = origin
        self.status = status
        self.kind = kind
        self.retry = retry
        self.write_outcome = write_outcome
        status_text = f", HTTP {status}" if status is not None else ""
        super().__init__(
            f"{self.operation.value} failed at {origin}{status_text}: "
            f"{kind.value}, retry={retry.value}, write={write_outcome.value}"
        )

    @property
    def transport(self) -> bool:
        return self.status is None and self.kind not in {
            TransportFaultKind.INVALID_RESPONSE,
            TransportFaultKind.INVALID_URL,
            TransportFaultKind.UNSUPPORTED_PROTOCOL,
        }

    @property
    def detail(self) -> str:
        return self.kind.value

    @property
    def aborts_origin(self) -> bool:
        return self.kind in {
            TransportFaultKind.TLS_CERTIFICATE,
            TransportFaultKind.ORIGIN_ABORTED,
        } or (
            self.kind
            in {
                TransportFaultKind.HTTP_STATUS,
                TransportFaultKind.INVALID_RESPONSE,
            }
            and self.retry is RetryClassification.PERMANENT
            and self.operation in {TransferOperation.MANIFEST, TransferOperation.PRESIGN}
        )


@dataclass(frozen=True)
class FetchedObject:
    body: bytes
    etag: str


@dataclass(frozen=True)
class PutResult:
    etag: str
    outcome: WriteOutcome


def _has_certificate_error(error: BaseException) -> bool:
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _write_outcome(operation: str | TransferOperation, outcome: WriteOutcome) -> WriteOutcome:
    if TransferOperation(operation) is TransferOperation.PUT:
        return outcome
    return WriteOutcome.NOT_APPLICABLE


def classify_exception(
    error: BaseException, *, operation: str | TransferOperation
) -> FaultClassification:
    """Classify retry value separately from PUT outcome evidence."""
    if _has_certificate_error(error):
        return FaultClassification(
            TransportFaultKind.TLS_CERTIFICATE,
            RetryClassification.PERMANENT,
            _write_outcome(operation, WriteOutcome.NOT_WRITTEN),
        )
    if isinstance(error, httpx.InvalidURL):
        kind = TransportFaultKind.INVALID_URL
        retry = RetryClassification.PERMANENT
        outcome = WriteOutcome.NOT_WRITTEN
    elif isinstance(error, httpx.UnsupportedProtocol):
        kind = TransportFaultKind.UNSUPPORTED_PROTOCOL
        retry = RetryClassification.PERMANENT
        outcome = WriteOutcome.NOT_WRITTEN
    elif isinstance(error, httpx.PoolTimeout):
        kind = TransportFaultKind.POOL_TIMEOUT
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.NOT_WRITTEN
    elif isinstance(error, httpx.ConnectTimeout):
        kind = TransportFaultKind.CONNECT_TIMEOUT
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.NOT_WRITTEN
    elif isinstance(error, httpx.ConnectError):
        kind = TransportFaultKind.CONNECT_ERROR
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.NOT_WRITTEN
    elif isinstance(error, httpx.WriteTimeout):
        kind = TransportFaultKind.WRITE_TIMEOUT
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.AMBIGUOUS
    elif isinstance(error, httpx.ReadTimeout):
        kind = TransportFaultKind.READ_TIMEOUT
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.AMBIGUOUS
    elif isinstance(error, httpx.WriteError):
        kind = TransportFaultKind.WRITE_ERROR
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.AMBIGUOUS
    elif isinstance(error, httpx.ReadError):
        kind = TransportFaultKind.READ_ERROR
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.AMBIGUOUS
    elif isinstance(error, httpx.RemoteProtocolError):
        kind = TransportFaultKind.REMOTE_PROTOCOL
        retry = RetryClassification.TRANSIENT
        outcome = WriteOutcome.AMBIGUOUS
    else:
        kind = TransportFaultKind.UNKNOWN
        retry = RetryClassification.PERMANENT
        outcome = WriteOutcome.AMBIGUOUS
    return FaultClassification(kind, retry, _write_outcome(operation, outcome))


def classify_status(status: int, *, operation: str | TransferOperation) -> FaultClassification:
    operation = TransferOperation(operation)
    retry = (
        RetryClassification.EXPIRED_CANDIDATE
        if status == _EXPIRED_STATUS and operation in {TransferOperation.GET, TransferOperation.PUT}
        else RetryClassification.TRANSIENT
        if status in _TRANSIENT_STATUSES
        else RetryClassification.PERMANENT
    )
    if operation is not TransferOperation.PUT:
        outcome = WriteOutcome.NOT_APPLICABLE
    elif 400 <= status < 500:
        outcome = WriteOutcome.NOT_WRITTEN
    else:
        outcome = WriteOutcome.AMBIGUOUS
    return FaultClassification(TransportFaultKind.HTTP_STATUS, retry, outcome)


def canonical_origin(url: str) -> str:
    """Return scheme, lowercased host, and effective port without URL secrets."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if not scheme or host is None:
            return "invalid-origin"
        port = parsed.port
    except (TypeError, ValueError):
        return "invalid-origin"
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return f"{scheme}://{host.lower()}{f':{port}' if port is not None else ''}"


class TransferSession:
    """One bounded pooled HTTP client plus operation-scoped origin circuits."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._owned = client is None
        self.client = client or httpx.Client(limits=_CLIENT_LIMITS)
        self._tripped_origins: set[str] = set()

    def __enter__(self) -> TransferSession:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self.client.close()

    def trip(self, url: str) -> None:
        self._tripped_origins.add(canonical_origin(url))

    def guard(self, url: str, operation: str | TransferOperation) -> str:
        origin = canonical_origin(url)
        if origin in self._tripped_origins:
            raise TransferBoundaryError(
                operation=operation,
                origin=origin,
                kind=TransportFaultKind.ORIGIN_ABORTED,
                retry=RetryClassification.PERMANENT,
                write_outcome=_write_outcome(operation, WriteOutcome.NOT_WRITTEN),
            )
        return origin

    def boundary_error(
        self, url: str, operation: str | TransferOperation, error: BaseException
    ) -> TransferBoundaryError:
        origin = canonical_origin(url)
        classification = classify_exception(error, operation=operation)
        if classification.kind is TransportFaultKind.TLS_CERTIFICATE:
            self.trip(url)
        return TransferBoundaryError(
            operation=operation,
            origin=origin,
            kind=classification.kind,
            retry=classification.retry,
            write_outcome=classification.write_outcome,
        )


@contextmanager
def operation_session(session: TransferSession | None = None) -> Iterator[TransferSession]:
    if session is not None:
        yield session
        return
    with TransferSession() as owned:
        yield owned


def resolve_api_url() -> str:
    env_url = os.environ.get("NAURO_API_URL") or ""
    config_url = str(load_config().get("api_url") or "")
    return (env_url or config_url or DEFAULT_API_URL).rstrip("/")


PRESIGN_BATCH_LIMIT = 200


def _get(session: TransferSession, url: str, operation: TransferOperation, **kwargs: Any):
    session.guard(url, operation)
    try:
        return session.client.get(url, **kwargs)
    except Exception as exc:
        error = session.boundary_error(url, operation, exc)
    raise error from None


def _post(session: TransferSession, url: str, operation: TransferOperation, **kwargs: Any):
    session.guard(url, operation)
    try:
        return session.client.post(url, **kwargs)
    except Exception as exc:
        error = session.boundary_error(url, operation, exc)
    raise error from None


def _put(session: TransferSession, url: str, **kwargs: Any):
    session.guard(url, TransferOperation.PUT)
    try:
        return session.client.put(url, **kwargs)
    except Exception as exc:
        error = session.boundary_error(url, TransferOperation.PUT, exc)
    raise error from None


def _status_error(
    session: TransferSession,
    url: str,
    operation: TransferOperation,
    status: int,
) -> TransferBoundaryError:
    classification = classify_status(status, operation=operation)
    error = TransferBoundaryError(
        operation=operation,
        origin=canonical_origin(url),
        status=status,
        kind=classification.kind,
        retry=classification.retry,
        write_outcome=classification.write_outcome,
    )
    if error.aborts_origin:
        session.trip(url)
    return error


def _invalid_response(
    session: TransferSession,
    url: str,
    operation: TransferOperation,
    status: int,
) -> TransferBoundaryError:
    error = TransferBoundaryError(
        operation=operation,
        origin=canonical_origin(url),
        status=status,
        kind=TransportFaultKind.INVALID_RESPONSE,
        retry=RetryClassification.PERMANENT,
        write_outcome=_write_outcome(operation, WriteOutcome.AMBIGUOUS),
    )
    if error.aborts_origin:
        session.trip(url)
    return error


def fetch_manifest(project_id: str, *, session: TransferSession | None = None) -> list[dict]:
    api_url = resolve_api_url()
    endpoint = f"{api_url}/sync/manifest"
    files: list[dict] = []
    cursor: str | None = None
    with operation_session(session) as active:
        while True:
            params: dict[str, str] = {"project_id": project_id}
            if cursor:
                params["cursor"] = cursor

            def call(token: str, params=params) -> httpx.Response:
                return _get(
                    active,
                    endpoint,
                    TransferOperation.MANIFEST,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_DEFAULT_API_TIMEOUT,
                )

            response = with_token_refresh(call, client=active.client)
            if response.status_code != 200:
                raise _status_error(
                    active,
                    endpoint,
                    TransferOperation.MANIFEST,
                    response.status_code,
                )
            try:
                body = response.json()
            except ValueError:
                raise _invalid_response(
                    active,
                    endpoint,
                    TransferOperation.MANIFEST,
                    response.status_code,
                ) from None
            page_files = body.get("files") if isinstance(body, dict) else None
            if not isinstance(page_files, list):
                raise _invalid_response(
                    active,
                    endpoint,
                    TransferOperation.MANIFEST,
                    response.status_code,
                )
            files.extend(page_files)
            next_cursor = body.get("next_cursor") if isinstance(body, dict) else None
            if not next_cursor:
                return files
            cursor = str(next_cursor)


def request_presigned_urls(
    project_id: str,
    operations: list[dict[str, str]],
    *,
    session: TransferSession | None = None,
) -> list[dict[str, Any]]:
    if not operations:
        return []
    api_url = resolve_api_url()
    endpoint = f"{api_url}/sync/presign"
    from nauro.sync.writer_credential import writer_headers

    upload_headers = (
        writer_headers(project_id, api_url)
        if any(op.get("verb") == "PUT" for op in operations)
        else {}
    )
    all_urls: list[dict[str, Any]] = []
    with operation_session(session) as active:
        for start in range(0, len(operations), PRESIGN_BATCH_LIMIT):
            chunk = operations[start : start + PRESIGN_BATCH_LIMIT]

            def call(token: str, chunk=chunk) -> httpx.Response:
                return _post(
                    active,
                    endpoint,
                    TransferOperation.PRESIGN,
                    json={"project_id": project_id, "operations": chunk},
                    headers={"Authorization": f"Bearer {token}", **upload_headers},
                    timeout=_DEFAULT_API_TIMEOUT,
                )

            response = with_token_refresh(call, client=active.client)
            if response.status_code != 200:
                raise _status_error(
                    active,
                    endpoint,
                    TransferOperation.PRESIGN,
                    response.status_code,
                )
            try:
                body = response.json()
            except ValueError:
                raise _invalid_response(
                    active,
                    endpoint,
                    TransferOperation.PRESIGN,
                    response.status_code,
                ) from None
            urls = body.get("urls") if isinstance(body, dict) else None
            if not isinstance(urls, list):
                raise _invalid_response(
                    active,
                    endpoint,
                    TransferOperation.PRESIGN,
                    response.status_code,
                )
            all_urls.extend(urls)
    return all_urls


class PresignedUrl(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verb: str
    path: str
    url: str


def _unreadable_entry(entry: Any) -> str:
    if not isinstance(entry, dict):
        return f"entry of type {type(entry).__name__}"
    return f"entry verb={entry.get('verb')!r}, path={entry.get('path')!r}"


def urls_by_path(entries: list[Any], verb: str) -> tuple[dict[str, str], tuple[str, ...]]:
    usable: dict[str, str] = {}
    skipped: list[str] = []
    for entry in entries:
        try:
            parsed = PresignedUrl.model_validate(entry)
        except ValidationError:
            skipped.append(_unreadable_entry(entry))
            continue
        if parsed.verb == verb:
            usable[parsed.path] = parsed.url
    return usable, tuple(skipped)


def fetch_via_presigned_url(url: str, *, session: TransferSession | None = None) -> FetchedObject:
    with operation_session(session) as active:
        response = _get(
            active,
            url,
            TransferOperation.GET,
            timeout=_DEFAULT_TRANSFER_TIMEOUT,
        )
    if response.status_code != 200:
        raise _status_error(active, url, TransferOperation.GET, response.status_code)
    return FetchedObject(response.content, response.headers.get("ETag", "").strip())


def put_via_presigned_url(
    url: str, payload: bytes, *, session: TransferSession | None = None
) -> PutResult:
    with operation_session(session) as active:
        response = _put(active, url, content=payload, timeout=_DEFAULT_TRANSFER_TIMEOUT)
    if not 200 <= response.status_code < 300:
        raise _status_error(active, url, TransferOperation.PUT, response.status_code)
    etag = response.headers.get("ETag", "").strip()
    return PutResult(
        etag=etag,
        outcome=WriteOutcome.VERIFIED if etag else WriteOutcome.WRITTEN_UNVERIFIED,
    )


__all__ = [
    "FaultClassification",
    "FetchedObject",
    "PRESIGN_BATCH_LIMIT",
    "PresignError",
    "PresignedUrl",
    "PutResult",
    "RetryClassification",
    "TransferBoundaryError",
    "TransferOperation",
    "TransferSession",
    "TransportFaultKind",
    "WriteOutcome",
    "canonical_origin",
    "classify_exception",
    "classify_status",
    "fetch_manifest",
    "fetch_via_presigned_url",
    "operation_session",
    "put_via_presigned_url",
    "request_presigned_urls",
    "resolve_api_url",
    "urls_by_path",
]
