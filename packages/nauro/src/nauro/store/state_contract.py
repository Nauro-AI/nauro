"""Strict state request identity and authenticated response evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.operations.update_state import plan_state_update
from nauro_core.provenance import validate_utc_timestamp
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
)

from nauro.store.submission_records import Digest, SubmissionRecordError


class StateTransportError(SubmissionRecordError):
    """The state response did not establish bound operation evidence."""


class ClosedModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class StateScope(ClosedModel):
    project_id: StrictStr
    user_id: StrictStr
    operation_kind: Literal["update_state"] = "update_state"
    operation_id: StrictStr

    @field_validator("project_id", "user_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="state_scope")

    @field_validator("operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.operation_id, value, field="operation_id")


class StatePayload(ClosedModel):
    operation: Literal["update_state"]
    delta: StrictStr
    expected_revision: StrictStr | None


def state_payload(delta: str, expected_revision: str | None = None) -> bytes:
    request = StatePayload(
        operation="update_state", delta=delta, expected_revision=expected_revision
    )
    return plan_state_update(
        delta=request.delta,
        expected_revision=request.expected_revision,
        state_current_bytes=None,
        state_history_bytes=None,
        legacy_state_bytes=None,
        updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
    ).payload_bytes


def read_state_payload(raw: str) -> StatePayload:
    payload = StatePayload.model_validate_json(raw, strict=True)
    if state_payload(payload.delta, payload.expected_revision) != raw.encode("utf-8"):
        raise ValueError("state payload is not canonical")
    return payload


class _Response(ClosedModel):
    version: StrictInt = Field(ge=1, le=1)
    scope: StateScope
    payload_digest: Digest

    @field_validator("unresolved", mode="before", check_fields=False)
    @classmethod
    def _boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("unresolved must be a boolean")
        return value


class StateCommitted(_Response):
    status: Literal["committed"]
    unresolved: Literal[False]
    receipt_json: StrictStr
    warning: StrictStr | None


class StateObserved(_Response):
    status: Literal["absent", "noop_observed"]
    unresolved: Literal[True]


class StateRevisionObserved(_Response):
    status: Literal["revision_conflict_observed"]
    unresolved: Literal[True]
    expected_revision: StrictStr
    current_revision: StrictStr

    @field_validator("expected_revision", "current_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.state_revision, value, field="state_revision")


class StateUnavailable(_Response):
    status: Literal["expired", "digest_conflict"]
    unresolved: Literal[False]


StateResult = StateCommitted | StateObserved | StateRevisionObserved | StateUnavailable
_RESPONSE: TypeAdapter[StateResult] = TypeAdapter(
    Annotated[StateResult, Field(discriminator="status")]
)


class _ReceiptDetails(ClosedModel):
    base_generation_id: StrictStr
    base_manifest_digest: Digest
    base_snapshot_digest: Digest
    source_revision: StrictStr
    previous_revision: StrictStr
    state_revision: Digest
    previous_history_revision: StrictStr
    history_revision: StrictStr
    snapshot_key: StrictStr
    snapshot_digest: Digest

    @field_validator("base_generation_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="base_generation_id")

    @field_validator(
        "source_revision", "previous_revision", "previous_history_revision", "history_revision"
    )
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.state_revision, value, field="state_revision")


class _Receipt(ClosedModel):
    receipt_id: StrictStr
    operation_kind: Literal["update_state"]
    operation_id: StrictStr
    result: Literal["committed"]
    committed_at: StrictStr
    artifact_digest: Digest
    generation_id: StrictStr
    target_kind: Literal["curated_state"]
    target_id: Literal["state_current.md"]
    details: _ReceiptDetails

    @field_validator("receipt_id", "generation_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="state_receipt")

    @field_validator("committed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="committed_at")


def _verify_receipt(raw: str, scope: StateScope, payload: StatePayload) -> None:
    if len(raw.encode("utf-8")) > 8192:
        raise ValueError("state receipt exceeds byte limit")
    receipt = _Receipt.model_validate_json(raw, strict=True)
    canonical = json.dumps(
        receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if canonical != raw or receipt.operation_id != scope.operation_id:
        raise ValueError("receipt bytes or operation differ")
    if receipt.generation_id == receipt.details.base_generation_id:
        raise ValueError("receipt must advance the generation")
    if (
        receipt.details.snapshot_key
        != f"generations/{scope.project_id}/{receipt.generation_id}/snapshot.json"
    ):
        raise ValueError("receipt snapshot differs")
    if (
        payload.expected_revision is not None
        and receipt.details.previous_revision != payload.expected_revision
    ):
        raise ValueError("receipt precondition differs")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def verify_state_response(
    raw: bytes, scope: StateScope, payload_json: str, *, lookup: bool = False
) -> StateResult:
    try:
        scope = StateScope.model_validate(scope)
        payload = read_state_payload(payload_json)
        if len(raw) > 64 * 1024:
            raise ValueError("response exceeds byte limit")
        result = _RESPONSE.validate_python(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        )
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if result.scope != scope or result.payload_digest != digest:
            raise ValueError("state response binding differs")
        if lookup and result.status in {"noop_observed", "revision_conflict_observed"}:
            raise ValueError("lookup cannot establish a no-write observation")
        if isinstance(result, StateRevisionObserved) and (
            result.expected_revision != payload.expected_revision
            or result.current_revision == result.expected_revision
        ):
            raise ValueError("revision conflict does not bind the request")
        if isinstance(result, StateCommitted):
            _verify_receipt(result.receipt_json, scope, payload)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise StateTransportError("The state response did not verify.") from exc
