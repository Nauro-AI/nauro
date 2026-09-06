"""Dormant authenticated transport for durable judgment submissions."""

from __future__ import annotations

import json
from typing import Literal

import httpx
from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.provenance import validate_utc_timestamp
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from nauro.auth import read_active_credentials
from nauro.store.submission_records import (
    Digest,
    JudgmentSubmission,
    JudgmentTransportResult,
    SubmissionActorMismatchError,
    SubmissionRecordError,
    SubmissionScope,
    require_submission_actor,
)


class JudgmentTransportError(SubmissionRecordError):
    """The authenticated transport did not establish a bound operation result."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _ReceiptDetails(_ClosedModel):
    decision_counter: StrictInt = Field(ge=1)
    fencing_token: StrictInt = Field(ge=1)
    manifest_digest: Digest
    plan_record_digest: Digest
    saga_id: StrictStr
    snapshot_digest: Digest
    snapshot_key: StrictStr

    @field_validator("saga_id")
    @classmethod
    def _saga_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="saga_id")


class _JudgmentReceipt(_ClosedModel):
    receipt_id: StrictStr
    operation_kind: Literal["judgment_commit"]
    operation_id: StrictStr
    result: Literal["committed"]
    committed_at: StrictStr
    artifact_digest: Digest
    generation_id: StrictStr
    target_kind: Literal["decision"]
    target_id: StrictStr
    details: _ReceiptDetails

    @field_validator("receipt_id", "generation_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="receipt_identity")

    @field_validator("committed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="committed_at")

    @field_validator("target_id")
    @classmethod
    def _target(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.audit_target_id, value, field="target_id")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


class _Response(JudgmentTransportResult):
    version: StrictInt = Field(ge=1, le=1)


def verify_judgment_response(
    raw: bytes, scope: SubmissionScope, payload_digest: str
) -> JudgmentTransportResult:
    try:
        if len(raw) > 64 * 1024:
            raise ValueError("response exceeds byte limit")
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        result = _Response.model_validate(parsed)
        if result.scope != scope or result.payload_digest != payload_digest:
            raise ValueError("response binding mismatch")
        if result.receipt_json is not None:
            _verify_receipt(result.receipt_json, scope)
        return JudgmentTransportResult.model_validate(result.model_dump(exclude={"version"}))
    except (ValueError, TypeError, RecursionError) as exc:
        raise JudgmentTransportError("The server response did not verify.") from exc


def _verify_receipt(receipt_json: str, scope: SubmissionScope) -> None:
    if len(receipt_json.encode("utf-8")) > 8192:
        raise ValueError("receipt exceeds byte limit")
    receipt = _JudgmentReceipt.model_validate_json(receipt_json, strict=True)
    canonical = json.dumps(
        receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if canonical != receipt_json or receipt.operation_id != scope.operation_id:
        raise ValueError("receipt identity or canonical bytes differ")
    if receipt.artifact_digest != receipt.details.manifest_digest:
        raise ValueError("receipt manifest differs")
    snapshot_key = f"generations/{scope.project_id}/{receipt.generation_id}/snapshot.json"
    if receipt.details.snapshot_key != snapshot_key:
        raise ValueError("receipt snapshot differs")


class HttpJudgmentTransport:
    """Use one caller-owned client and a trusted origin without automatic retries."""

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
            raise JudgmentTransportError("Judgment transport requires a trusted HTTPS origin.")
        self._base_url = str(url).rstrip("/")
        self._client = client

    def _request(
        self, route: str, scope: SubmissionScope, digest: str, payload: str | None = None
    ) -> JudgmentTransportResult:
        credentials = read_active_credentials()
        if credentials.user_id != scope.user_id:
            raise SubmissionActorMismatchError("The active account does not own this submission.")
        body: dict[str, object] = {
            "version": 1,
            "project_id": scope.project_id,
            "expected_user_id": scope.user_id,
            "operation_id": scope.operation_id,
            "payload_digest": digest,
        }
        if payload is not None:
            body["approved_payload"] = payload
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
                    raise JudgmentTransportError("The server did not return an operation result.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 64 * 1024:
                        raise JudgmentTransportError("The server response exceeds the byte limit.")
        except httpx.HTTPError as exc:
            raise JudgmentTransportError(
                "The operation result is unknown. Look up its identity."
            ) from exc
        require_submission_actor(scope.user_id)
        return verify_judgment_response(bytes(raw), scope, digest)

    def submit(self, record: JudgmentSubmission) -> JudgmentTransportResult:
        record = JudgmentSubmission.model_validate(record)
        return self._request(
            "/judgments/submit", record.scope, record.payload_digest, record.approved_payload
        )

    def lookup(self, scope: SubmissionScope, payload_digest: str) -> JudgmentTransportResult:
        scope = SubmissionScope.model_validate(scope)
        return self._request("/judgments/lookup", scope, payload_digest)
