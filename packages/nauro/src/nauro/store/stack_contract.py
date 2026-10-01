"""Strict stack request identity and authenticated response evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.operations.update_stack import compute_stack_revision, update_stack
from nauro_core.provenance import validate_utc_timestamp
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from nauro.store.submission_records import Digest, SubmissionRecordError


class StackTransportError(SubmissionRecordError):
    """The stack response did not establish bound operation evidence."""


class ClosedModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class StackScope(ClosedModel):
    project_id: StrictStr
    user_id: StrictStr
    operation_kind: Literal["update_stack"] = "update_stack"
    operation_id: StrictStr

    @field_validator("project_id", "user_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="stack_scope")

    @field_validator("operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.operation_id, value, field="operation_id")


class StackPayload(ClosedModel):
    operation: Literal["update_stack"]
    content: StrictStr
    expected_revision: StrictStr | None


def stack_payload(content: str, expected_revision: str | None = None) -> bytes:
    request = StackPayload(
        operation="update_stack", content=content, expected_revision=expected_revision
    )
    return update_stack(request.content, request.expected_revision).payload_bytes


def read_stack_payload(raw: str) -> StackPayload:
    payload = StackPayload.model_validate_json(raw, strict=True)
    if stack_payload(payload.content, payload.expected_revision) != raw.encode("utf-8"):
        raise ValueError("stack payload is not canonical")
    return payload


class _Response(ClosedModel):
    version: StrictInt = Field(ge=1, le=1)
    scope: StackScope
    payload_digest: Digest

    @field_validator("unresolved", mode="before", check_fields=False)
    @classmethod
    def _boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("unresolved must be a boolean")
        return value


class StackCommitted(_Response):
    status: Literal["committed"]
    unresolved: Literal[False]
    receipt_json: StrictStr


class StackObserved(_Response):
    status: Literal["absent"]
    unresolved: Literal[True]


class StackRevisionObserved(_Response):
    status: Literal["revision_conflict_observed"]
    unresolved: Literal[True]
    expected_revision: StrictStr
    current_revision: StrictStr

    @field_validator("expected_revision", "current_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.stack_revision, value, field="stack_revision")


class StackUnavailable(_Response):
    status: Literal["expired", "digest_conflict"]
    unresolved: Literal[False]


_REFUSAL_CODES = {
    400: {"invalid_request", "payload_digest_mismatch", "submission_rejected"},
    401: {"authentication_required", "invalid_token"},
    403: {
        "insufficient_scope",
        "actor_mismatch",
        "project_not_selected",
        "pre_team_required",
        "owner_required",
        "forbidden",
    },
    409: {
        "single_writer_refused",
        "generation_authority_required",
        "shared_generation_authority_required",
        "generation_required",
        "shared_representation_required",
    },
    413: {"request_too_large"},
}


_PREPUBLICATION_REFUSALS = {
    "authentication_required",
    "invalid_token",
    "insufficient_scope",
    "actor_mismatch",
    "project_not_selected",
    "pre_team_required",
    "owner_required",
    "single_writer_refused",
    "generation_authority_required",
    "shared_generation_authority_required",
    "generation_required",
    "shared_representation_required",
    "request_too_large",
    "payload_digest_mismatch",
    "submission_rejected",
}


class StackRefused(_Response):
    status: Literal["refused"] = "refused"
    unresolved: bool = True
    http_status: StrictInt
    server_code: StrictStr
    request_mode: Literal["submit", "lookup"]

    @model_validator(mode="after")
    def _refusal(self) -> StackRefused:
        if self.server_code not in _REFUSAL_CODES.get(self.http_status, set()):
            raise ValueError("unknown stack refusal")
        if not self.unresolved and (not self.before_publication):
            raise ValueError("stack refusal does not establish a no-write outcome")
        return self

    @property
    def before_publication(self) -> bool:
        # The server can emit forbidden or invalid_request during post-write receipt lookup.
        return self.request_mode == "submit" and self.server_code in _PREPUBLICATION_REFUSALS


StackResult = (
    StackCommitted | StackObserved | StackRevisionObserved | StackUnavailable | StackRefused
)
_RESPONSE: TypeAdapter[StackResult] = TypeAdapter(
    Annotated[StackResult, Field(discriminator="status")]
)


class _ReceiptDetails(ClosedModel):
    base_generation_id: StrictStr
    base_manifest_digest: Digest
    base_snapshot_digest: Digest
    previous_revision: StrictStr
    stack_revision: Digest
    snapshot_key: StrictStr
    snapshot_digest: Digest

    @field_validator("base_generation_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="base_generation_id")

    @field_validator("previous_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.stack_revision, value, field="stack_revision")


class _Receipt(ClosedModel):
    receipt_id: StrictStr
    operation_kind: Literal["update_stack"]
    operation_id: StrictStr
    result: Literal["committed"]
    committed_at: StrictStr
    artifact_digest: Digest
    generation_id: StrictStr
    target_kind: Literal["curated_stack"]
    target_id: Literal["stack.md"]
    details: _ReceiptDetails

    @field_validator("receipt_id", "generation_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="stack_receipt")

    @field_validator("committed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="committed_at")


def _verify_receipt(raw: str, scope: StackScope, payload: StackPayload) -> None:
    if len(raw.encode("utf-8")) > 1193:
        raise ValueError("stack receipt exceeds byte limit")
    receipt = _Receipt.model_validate_json(raw, strict=True)
    canonical = json.dumps(
        receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if canonical != raw or receipt.operation_id != scope.operation_id:
        raise ValueError("receipt bytes or operation differ")
    if receipt.details.stack_revision != compute_stack_revision(payload.content.encode("utf-8")):
        raise ValueError("receipt content revision differs")
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


def verify_stack_response(
    raw: bytes, scope: StackScope, payload_json: str, *, lookup: bool = False, local: bool = False
) -> StackResult:
    try:
        scope = StackScope.model_validate(scope)
        payload = read_stack_payload(payload_json)
        if len(raw) > 64 * 1024:
            raise ValueError("response exceeds byte limit")
        result = _RESPONSE.validate_python(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        )
        if isinstance(result, StackRefused) and not local:
            raise StackTransportError("Refusals require an HTTP refusal response.")
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if result.scope != scope or result.payload_digest != digest:
            raise ValueError("stack response binding differs")
        if lookup and result.status == "revision_conflict_observed":
            raise ValueError("lookup cannot establish a no-write observation")
        if isinstance(result, StackRevisionObserved) and (
            result.expected_revision != payload.expected_revision
            or result.current_revision == result.expected_revision
        ):
            raise ValueError("revision conflict does not bind the request")
        if isinstance(result, StackCommitted):
            _verify_receipt(result.receipt_json, scope, payload)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise StackTransportError("The stack response did not verify.") from exc


def verify_stack_refusal(
    raw: bytes, status: int, scope: StackScope, payload_digest: str, *, lookup: bool
) -> StackRefused:
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(body, dict) or set(body) != {"detail"}:
            raise StackTransportError("The stack refusal body did not verify.")
        return StackRefused(
            version=1,
            scope=scope,
            payload_digest=payload_digest,
            http_status=status,
            server_code=body["detail"],
            request_mode="lookup" if lookup else "submit",
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise StackTransportError("The stack refusal did not verify.") from exc
