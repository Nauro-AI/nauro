"""Dormant HTTPS adapter for original question payloads and verified receipts."""

from __future__ import annotations

import httpx

from nauro.auth import read_active_credentials
from nauro.store.question_contract import (
    QuestionResult,
    QuestionTransportError,
    verify_question_response,
)
from nauro.store.question_records import QuestionSubmission
from nauro.store.submission_records import SubmissionActorMismatchError, require_submission_actor


class HttpQuestionTransport:
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
            raise QuestionTransportError("Question transport requires a trusted HTTPS origin.")
        self._base_url = str(url).rstrip("/")
        self._client = client

    def _request(self, record: QuestionSubmission, *, lookup: bool) -> QuestionResult:
        record = QuestionSubmission.model_validate(record)
        credentials = read_active_credentials()
        if credentials.user_id != record.scope.user_id:
            raise SubmissionActorMismatchError(
                "The active account does not own this question submission."
            )
        body = {
            "version": 1,
            "project_id": record.scope.project_id,
            "expected_user_id": record.scope.user_id,
            "operation_id": record.scope.operation_id,
            "payload_digest": record.payload_digest,
            "payload_json": record.payload_json,
        }
        route = "/questions/lookup" if lookup else "/questions/submit"
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
                    raise QuestionTransportError("The server did not return a question result.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 64 * 1024:
                        raise QuestionTransportError(
                            "The question response exceeds its byte limit."
                        )
        except httpx.HTTPError as exc:
            raise QuestionTransportError(
                "The question outcome is unresolved. Look up its original identity."
            ) from exc
        require_submission_actor(record.scope.user_id)
        return verify_question_response(
            bytes(raw), record.scope, record.payload_json, lookup=lookup
        )

    def submit(self, record: QuestionSubmission) -> QuestionResult:
        return self._request(record, lookup=False)

    def lookup(self, record: QuestionSubmission) -> QuestionResult:
        return self._request(record, lookup=True)
