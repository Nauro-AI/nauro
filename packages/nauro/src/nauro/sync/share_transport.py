"""Dormant HTTPS adapter for original share payloads and verified receipts."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from nauro.auth import ActiveCredentials, read_active_credentials
from nauro.store.share_contract import (
    ShareResult,
    ShareTransportError,
    verify_share_refusal,
    verify_share_response,
)
from nauro.store.share_records import ShareSubmission
from nauro.store.submission_records import SubmissionActorMismatchError, require_submission_actor
from nauro.sync.generation_credentials import GenerationConnection


class HttpShareTransport:
    def __init__(
        self,
        base_url: str,
        client: httpx.Client,
        *,
        connection: GenerationConnection | None = None,
        credentials: Callable[[], ActiveCredentials] | None = None,
        require_actor: Callable[[str], None] | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ShareTransportError("Share transport requires a trusted HTTPS origin.")
        if connection is not None and url.copy_with(path="/") != httpx.URL(
            connection.endpoint.removesuffix("/mcp")
        ).copy_with(path="/"):
            raise ShareTransportError("The share transport origin does not match its connection.")
        if connection is not None and credentials is None:
            raise ShareTransportError("A bound share transport requires a credentials provider.")
        self._base_url = str(url).rstrip("/")
        self._client = client
        self._connection = connection.binding() if connection is not None else None
        self._credentials = read_active_credentials if credentials is None else credentials
        self._require_actor = require_actor or require_submission_actor

    def validate_binding(self, record: ShareSubmission) -> None:
        if record.connection != self._connection:
            raise ShareTransportError("The saved share connection does not match this transport.")

    def _request(self, record: ShareSubmission, *, lookup: bool) -> ShareResult:
        record = ShareSubmission.model_validate(record)
        self.validate_binding(record)
        credentials = self._credentials()
        if credentials.user_id != record.scope.user_id:
            raise SubmissionActorMismatchError(
                "The active account does not own this share submission."
            )
        body = {
            "version": 1,
            "project_id": record.scope.project_id,
            "expected_user_id": record.scope.user_id,
            "operation_id": record.scope.operation_id,
            "payload_digest": record.payload_digest,
            "payload_json": record.payload_json,
        }
        route = "/share/lookup" if lookup else "/share/submit"
        try:
            with self._client.stream(
                "POST",
                self._base_url + route,
                json=body,
                headers={"Authorization": f"Bearer {credentials.access_token}"},
                timeout=25,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 64 * 1024:
                        raise ShareTransportError("The share response exceeds its byte limit.")
        except httpx.HTTPError as exc:
            raise ShareTransportError(
                "The share outcome is unresolved. Look up its original identity."
            ) from exc
        self._require_actor(record.scope.user_id)
        if status != 200:
            return verify_share_refusal(
                bytes(raw), status, record.scope, record.payload_digest, lookup=lookup
            )
        return verify_share_response(bytes(raw), record.scope, record.payload_json, lookup=lookup)

    def submit(self, record: ShareSubmission) -> ShareResult:
        return self._request(record, lookup=False)

    def lookup(self, record: ShareSubmission) -> ShareResult:
        return self._request(record, lookup=True)
