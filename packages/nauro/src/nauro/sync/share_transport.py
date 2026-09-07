"""Dormant HTTPS adapter for original share payloads and verified receipts."""

from __future__ import annotations

import httpx

from nauro.auth import read_active_credentials
from nauro.store.share_contract import ShareResult, ShareTransportError, verify_share_response
from nauro.store.share_records import ShareSubmission
from nauro.store.submission_records import SubmissionActorMismatchError, require_submission_actor


class HttpShareTransport:
    def __init__(self, base_url: str, client: httpx.Client) -> None:
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
        self._base_url = str(url).rstrip("/")
        self._client = client

    def _request(self, record: ShareSubmission, *, lookup: bool) -> ShareResult:
        record = ShareSubmission.model_validate(record)
        credentials = read_active_credentials()
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
                if response.status_code != 200:
                    raise ShareTransportError("The server did not return a share result.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 64 * 1024:
                        raise ShareTransportError("The share response exceeds its byte limit.")
        except httpx.HTTPError as exc:
            raise ShareTransportError(
                "The share outcome is unresolved. Look up its original identity."
            ) from exc
        require_submission_actor(record.scope.user_id)
        return verify_share_response(bytes(raw), record.scope, record.payload_json, lookup=lookup)

    def submit(self, record: ShareSubmission) -> ShareResult:
        return self._request(record, lookup=False)

    def lookup(self, record: ShareSubmission) -> ShareResult:
        return self._request(record, lookup=True)
