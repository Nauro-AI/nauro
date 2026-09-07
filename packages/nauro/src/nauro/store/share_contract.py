"""Strict share request identity and authenticated response evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.operations.share_context import share_context
from nauro_core.provenance import validate_utc_timestamp
from nauro_core.questions import validate_question_id
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


class ShareTransportError(SubmissionRecordError):
    """The share response did not establish bound operation evidence."""


class ClosedModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ShareScope(ClosedModel):
    project_id: StrictStr
    user_id: StrictStr
    operation_kind: Literal["share_context"] = "share_context"
    operation_id: StrictStr

    @field_validator("project_id", "user_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="share_scope")

    @field_validator("operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.operation_id, value, field="operation_id")


class SharePayload(ClosedModel):
    operation: Literal["share_context"]
    slug: StrictStr
    content: StrictStr
    pointer_kind: StrictStr
    summary: StrictStr


def share_payload(slug: str, content: str, pointer_kind: str, summary: str) -> bytes:
    request = SharePayload(
        operation="share_context",
        slug=slug,
        content=content,
        pointer_kind=pointer_kind,
        summary=summary,
    )
    return share_context(
        request.slug, request.content, request.pointer_kind, request.summary
    ).payload_bytes


def read_share_payload(raw: str) -> SharePayload:
    payload = SharePayload.model_validate_json(raw, strict=True)
    if share_payload(
        payload.slug, payload.content, payload.pointer_kind, payload.summary
    ) != raw.encode("utf-8"):
        raise ValueError("share payload is not canonical")
    return payload


class _Response(ClosedModel):
    version: StrictInt = Field(ge=1, le=1)
    scope: ShareScope
    payload_digest: Digest

    @field_validator("unresolved", mode="before", check_fields=False)
    @classmethod
    def _boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("unresolved must be a boolean")
        return value


class ShareCommitted(_Response):
    status: Literal["committed"]
    unresolved: Literal[False]
    receipt_json: StrictStr


class ShareObserved(_Response):
    status: Literal["absent"]
    unresolved: Literal[True]


class ShareSlugObserved(_Response):
    status: Literal["slug_conflict_observed"]
    unresolved: Literal[True]
    slug: StrictStr
    suggested_slug: StrictStr

    @field_validator("slug", "suggested_slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.brief_slug, value, field="slug")


class ShareUnavailable(_Response):
    status: Literal["expired", "digest_conflict"]
    unresolved: Literal[False]


ShareResult = ShareCommitted | ShareObserved | ShareSlugObserved | ShareUnavailable
_RESPONSE: TypeAdapter[ShareResult] = TypeAdapter(
    Annotated[ShareResult, Field(discriminator="status")]
)


class _ReceiptDetails(ClosedModel):
    path: StrictStr
    question_id: StrictStr
    question_event_id: StrictStr
    question_created_at: StrictStr
    base_generation_id: StrictStr
    base_manifest_digest: Digest
    base_snapshot_digest: Digest
    brief_digest: Digest
    pointer_digest: Digest
    provenance_digest: Digest
    snapshot_key: StrictStr
    snapshot_digest: Digest

    @field_validator("base_generation_id", "question_event_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="share_receipt")

    @field_validator("question_id")
    @classmethod
    def _question(cls, value: str) -> str:
        return validate_question_id(value)

    @field_validator("question_created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="question_created_at")


class _Receipt(ClosedModel):
    receipt_id: StrictStr
    operation_kind: Literal["share_context"]
    operation_id: StrictStr
    result: Literal["committed"]
    committed_at: StrictStr
    artifact_digest: Digest
    generation_id: StrictStr
    target_kind: Literal["shared_brief"]
    target_id: StrictStr
    details: _ReceiptDetails

    @field_validator("receipt_id", "generation_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="share_receipt")

    @field_validator("committed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="committed_at")


def _verify_receipt(raw: str, scope: ShareScope, payload: SharePayload) -> None:
    if len(raw.encode("utf-8")) > 2661:
        raise ValueError("share receipt exceeds byte limit")
    receipt = _Receipt.model_validate_json(raw, strict=True)
    canonical = json.dumps(
        receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if canonical != raw or receipt.operation_id != scope.operation_id:
        raise ValueError("receipt bytes or operation differ")
    if receipt.details.brief_digest != hashlib.sha256(payload.content.encode("utf-8")).hexdigest():
        raise ValueError("receipt brief digest differs")
    if receipt.target_id != payload.slug or receipt.details.path != f"context/{payload.slug}.md":
        raise ValueError("receipt brief target differs")
    if receipt.details.question_created_at != receipt.committed_at:
        raise ValueError("receipt question timestamp differs")
    if receipt.generation_id == receipt.details.base_generation_id:
        raise ValueError("receipt must advance the generation")
    if (
        receipt.details.snapshot_key
        != f"generations/{scope.project_id}/{receipt.generation_id}/snapshot.json"
    ):
        raise ValueError("receipt snapshot differs")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def verify_share_response(
    raw: bytes, scope: ShareScope, payload_json: str, *, lookup: bool = False
) -> ShareResult:
    try:
        scope = ShareScope.model_validate(scope)
        payload = read_share_payload(payload_json)
        if len(raw) > 64 * 1024:
            raise ValueError("response exceeds byte limit")
        result = _RESPONSE.validate_python(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        )
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if result.scope != scope or result.payload_digest != digest:
            raise ValueError("share response binding differs")
        if isinstance(result, ShareSlugObserved) and (
            result.slug != payload.slug or result.suggested_slug == result.slug
        ):
            raise ValueError("slug observation does not bind the request")
        if isinstance(result, ShareCommitted):
            _verify_receipt(result.receipt_json, scope, payload)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise ShareTransportError("The share response did not verify.") from exc
