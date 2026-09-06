"""Strict question payloads and authenticated receipt evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from nauro_core.constants import MAX_CONTEXT_LENGTH, MAX_QUESTION_LENGTH, POINTER_FLAG_PREFIXES
from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.provenance import validate_utc_timestamp
from nauro_core.questions import (
    InvalidQuestionIdentifier,
    format_question_id,
    parse_question_id,
    validate_legacy_question_id,
    validate_question_id,
)
from nauro_core.validation import envelope_token_message
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


class QuestionTransportError(SubmissionRecordError):
    """The question response did not establish bound evidence."""


class ClosedModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


def _target(value: str) -> str:
    try:
        return format_question_id(parse_question_id(value))
    except InvalidQuestionIdentifier:
        return validate_legacy_question_id(value)


class QuestionScope(ClosedModel):
    project_id: StrictStr
    user_id: StrictStr
    operation_kind: Literal["flag_question"] = "flag_question"
    operation_id: StrictStr

    @field_validator("project_id", "user_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="question_scope")

    @field_validator("operation_id")
    @classmethod
    def _operation(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.operation_id, value, field="operation_id")


class AppendPayload(ClosedModel):
    question: StrictStr
    context: StrictStr | None
    targets: tuple[StrictStr, ...]

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        if not value.strip() or len(value) > MAX_QUESTION_LENGTH:
            raise ValueError("invalid question length")
        if value.lstrip().startswith(POINTER_FLAG_PREFIXES):
            raise ValueError("discovery pointers require share_context")
        return _line(value, "question")

    @field_validator("context")
    @classmethod
    def _context(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value) > MAX_CONTEXT_LENGTH:
                raise ValueError("invalid context length")
            _line(value, "context")
        return value


def _line(value: str, field: str) -> str:
    if "\n" in value or "\r" in value or envelope_token_message(value, field) is not None:
        raise ValueError("invalid question line")
    return value


class ResolutionPayload(ClosedModel):
    action: Literal["resolve"]
    resolved_by: StrictStr
    targets: tuple[StrictStr, ...] = Field(max_length=64)


QuestionPayload = AppendPayload | ResolutionPayload
_PAYLOAD: TypeAdapter[QuestionPayload] = TypeAdapter(QuestionPayload)


def _canonical(payload: QuestionPayload) -> bytes:
    data = payload.model_dump(mode="json")
    data["targets"] = [_target(value) for value in payload.targets]
    raw = (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if isinstance(payload, ResolutionPayload) and len(raw) > 16_384:
        raise ValueError("resolution payload exceeds byte limit")
    return raw


def question_payload(
    question: str, context: str | None = None, targets: tuple[str, ...] = ()
) -> bytes:
    return _canonical(AppendPayload(question=question, context=context, targets=targets))


def resolution_payload(targets: tuple[str, ...], resolved_by: str) -> bytes:
    return _canonical(ResolutionPayload(action="resolve", targets=targets, resolved_by=resolved_by))


def read_question_payload(raw: str) -> QuestionPayload:
    payload = _PAYLOAD.validate_json(raw, strict=True)
    if _canonical(payload) != raw.encode("utf-8"):
        raise ValueError("question payload is not canonical")
    return payload


class _Response(ClosedModel):
    version: StrictInt = Field(ge=1, le=1)
    scope: QuestionScope
    payload_digest: Digest
    action: Literal["append", "resolve"]

    @field_validator("unresolved", mode="before", check_fields=False)
    @classmethod
    def _boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("unresolved must be a boolean")
        return value


class ResolutionDiagnostics(ClosedModel):
    requested_question_ids: tuple[StrictStr, ...]
    resolved_question_ids: tuple[StrictStr, ...]
    resolved_by: StrictStr
    relocated_ids: tuple[StrictStr, ...]
    skipped_prose_ids: tuple[StrictStr, ...]

    @field_validator(
        "requested_question_ids", "resolved_question_ids", "relocated_ids", "skipped_prose_ids"
    )
    @classmethod
    def _ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_target(value) != value for value in values):
            raise ValueError("diagnostic identifier is not canonical")
        return values


class QuestionCommitted(_Response):
    action: Literal["append"]
    status: Literal["committed"]
    unresolved: Literal[False]
    receipt_json: StrictStr


class ResolutionCommitted(_Response):
    action: Literal["resolve"]
    status: Literal["committed"]
    unresolved: Literal[False]
    receipt_json: StrictStr
    diagnostics: ResolutionDiagnostics


class QuestionAbsent(_Response):
    status: Literal["absent"]
    unresolved: Literal[True]


class QuestionNoChange(_Response):
    action: Literal["resolve"]
    status: Literal["no_change_observed"]
    unresolved: Literal[True]
    diagnostics: ResolutionDiagnostics


class QuestionUnavailable(_Response):
    status: Literal["expired", "digest_conflict"]
    unresolved: Literal[False]


QuestionResult = (
    QuestionCommitted
    | ResolutionCommitted
    | QuestionAbsent
    | QuestionNoChange
    | QuestionUnavailable
)
_RESPONSE: TypeAdapter[QuestionResult] = TypeAdapter(QuestionResult)


class _SnapshotDetails(ClosedModel):
    snapshot_key: StrictStr
    snapshot_digest: Digest


class _AppendDetails(_SnapshotDetails):
    question_event_id: StrictStr
    question_created_at: StrictStr


class _ResolutionDetails(_SnapshotDetails):
    resolution_row_digest: Digest


class _Receipt(ClosedModel):
    receipt_id: StrictStr
    operation_kind: Literal["flag_question"]
    operation_id: StrictStr
    result: Literal["committed", "resolved"]
    committed_at: StrictStr
    artifact_digest: Digest
    generation_id: StrictStr
    target_kind: Literal["question", "question_resolution"]
    target_id: StrictStr
    details: _AppendDetails | _ResolutionDetails

    @field_validator("receipt_id", "generation_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="question_receipt")

    @field_validator("committed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="committed_at")


def _verify_receipt(raw: str, scope: QuestionScope, payload: QuestionPayload) -> None:
    limit = 8192 if isinstance(payload, AppendPayload) else 782
    if len(raw.encode("utf-8")) > limit:
        raise ValueError("question receipt exceeds byte limit")
    receipt = _Receipt.model_validate_json(raw, strict=True)
    canonical = json.dumps(
        receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if raw != canonical or receipt.operation_id != scope.operation_id:
        raise ValueError("receipt bytes or identity differ")
    if (
        receipt.details.snapshot_key
        != f"generations/{scope.project_id}/{receipt.generation_id}/snapshot.json"
    ):
        raise ValueError("receipt snapshot differs")
    if isinstance(payload, AppendPayload):
        if (
            receipt.result != "committed"
            or receipt.target_kind != "question"
            or not isinstance(receipt.details, _AppendDetails)
        ):
            raise ValueError("append receipt shape differs")
        validate_question_id(receipt.target_id)
        validate_identifier(
            IdentifierKind.ulid, receipt.details.question_event_id, field="question_event_id"
        )
        if receipt.details.question_created_at != receipt.committed_at:
            raise ValueError("question timestamp differs")
    elif (
        receipt.result != "resolved"
        or receipt.target_kind != "question_resolution"
        or not isinstance(receipt.details, _ResolutionDetails)
    ):
        raise ValueError("resolution receipt shape differs")
    else:
        validate_identifier(IdentifierKind.ulid, receipt.target_id, field="resolution_event_id")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def verify_question_response(
    raw: bytes, scope: QuestionScope, payload_json: str, *, lookup: bool = False
) -> QuestionResult:
    try:
        scope = QuestionScope.model_validate(scope)
        payload = read_question_payload(payload_json)
        if len(raw) > 64 * 1024:
            raise ValueError("response exceeds byte limit")
        json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        result = _RESPONSE.validate_json(raw, strict=True)
        action = "append" if isinstance(payload, AppendPayload) else "resolve"
        if (
            result.scope != scope
            or result.payload_digest != hashlib.sha256(payload_json.encode()).hexdigest()
            or result.action != action
        ):
            raise ValueError("question response binding differs")
        if isinstance(result, (ResolutionCommitted, QuestionNoChange)):
            if (
                not isinstance(payload, ResolutionPayload)
                or result.diagnostics.resolved_by != payload.resolved_by
            ):
                raise ValueError("resolution diagnostics differ from request")
        if isinstance(result, QuestionNoChange):
            if (
                lookup
                or result.diagnostics.resolved_question_ids
                or result.diagnostics.relocated_ids
            ):
                raise ValueError("invalid no-change observation")
        if isinstance(result, (QuestionCommitted, ResolutionCommitted)):
            _verify_receipt(result.receipt_json, scope, payload)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise QuestionTransportError("The question response did not verify.") from exc
