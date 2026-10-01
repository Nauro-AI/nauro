"""Dormant HTTPS adapter for original stack payloads and verified receipts."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from nauro.auth import ActiveCredentials, read_active_credentials
from nauro.store.stack_contract import (
    StackResult,
    StackTransportError,
    verify_stack_refusal,
    verify_stack_response,
)
from nauro.store.stack_records import StackSubmission
from nauro.store.submission_records import SubmissionActorMismatchError, require_submission_actor
from nauro.sync.generation_credentials import GenerationConnection


class HttpStackTransport:
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
            raise StackTransportError("Stack transport requires a trusted HTTPS origin.")
        if connection is not None and url.copy_with(path="/") != httpx.URL(
            connection.endpoint.removesuffix("/mcp")
        ).copy_with(path="/"):
            raise StackTransportError("The stack transport origin does not match its connection.")
        self._base_url = str(url).rstrip("/")
        self._client = client
        self._connection = connection.binding() if connection is not None else None
        self._credentials = credentials or read_active_credentials
        self._require_actor = require_actor or require_submission_actor

    def _request(self, record: StackSubmission, *, lookup: bool) -> StackResult:
        record = StackSubmission.model_validate(record)
        if record.connection != self._connection:
            raise StackTransportError("The saved stack connection does not match this transport.")
        credentials = self._credentials()
        if credentials.user_id != record.scope.user_id:
            raise SubmissionActorMismatchError(
                "The active account does not own this stack submission."
            )
        body = {
            "version": 1,
            "project_id": record.scope.project_id,
            "expected_user_id": record.scope.user_id,
            "operation_id": record.scope.operation_id,
            "payload_digest": record.payload_digest,
            "payload_json": record.payload_json,
        }
        route = "/stack/lookup" if lookup else "/stack/submit"
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
                        raise StackTransportError("The stack response exceeds its byte limit.")
        except httpx.HTTPError as exc:
            raise StackTransportError(
                "The stack outcome is unresolved. Look up its original identity."
            ) from exc
        self._require_actor(record.scope.user_id)
        if status != 200:
            return verify_stack_refusal(
                bytes(raw), status, record.scope, record.payload_digest, lookup=lookup
            )
        return verify_stack_response(bytes(raw), record.scope, record.payload_json, lookup=lookup)

    def submit(self, record: StackSubmission) -> StackResult:
        return self._request(record, lookup=False)

    def lookup(self, record: StackSubmission) -> StackResult:
        return self._request(record, lookup=True)
