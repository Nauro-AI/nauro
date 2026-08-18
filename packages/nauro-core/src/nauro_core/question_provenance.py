"""Strict immutable codec for the questions-provenance.json sidecar."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.provenance import validate_utc_timestamp
from nauro_core.questions import (
    InvalidLegacyQuestionIdentifier,
    InvalidQuestionIdentifier,
    validate_legacy_question_id,
    validate_question_id,
)

QUESTION_PROVENANCE_SCHEMA_VERSION = 1


class QuestionProvenanceError(ValueError):
    """Base class for closed question-provenance codec failures."""

    classification: ClassVar[str]

    def __init__(self, message: str) -> None:
        self.classification = type(self).classification
        super().__init__(message)


class QuestionProvenanceInvalidUtf8(QuestionProvenanceError):
    classification = "question_provenance_invalid_utf8"


class QuestionProvenanceMalformedJson(QuestionProvenanceError):
    classification = "question_provenance_json_malformed"


class QuestionProvenanceDuplicateKey(QuestionProvenanceError):
    classification = "question_provenance_duplicate_key"


class QuestionProvenanceVersionUnsupported(QuestionProvenanceError):
    classification = "question_provenance_version_unsupported"


class QuestionProvenanceSchemaInvalid(QuestionProvenanceError):
    classification = "question_provenance_schema_invalid"


class QuestionProvenanceNoncanonical(QuestionProvenanceError):
    classification = "question_provenance_noncanonical"


class QuestionProvenanceConflict(QuestionProvenanceError):
    classification = "question_provenance_conflict"


class QuestionProvenanceLegacyMissing(QuestionProvenanceConflict):
    classification = "question_provenance_legacy_missing"


class QuestionProvenanceRecord(BaseModel):
    """Immutable actor, time, and event provenance for one question."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    actor_id: StrictStr
    created_at: StrictStr
    event_id: StrictStr

    @field_validator("actor_id")
    @classmethod
    def actor_id_is_ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="actor_id")

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="created_at")

    @field_validator("event_id")
    @classmethod
    def event_id_is_ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="event_id")


class LegacyQuestionProvenanceRecord(QuestionProvenanceRecord):
    """Immutable legacy provenance with its optional canonical Q-form alias."""

    current_question_id: StrictStr | None

    @field_validator("current_question_id")
    @classmethod
    def current_id_is_canonical(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_question_id(value, field="current_question_id")


class QuestionProvenanceDocument(BaseModel):
    """Immutable schema-1 question-provenance projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    by_question_id: Mapping[str, QuestionProvenanceRecord]
    by_legacy_id: Mapping[str, LegacyQuestionProvenanceRecord]

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_integer_one(cls, value: object) -> object:
        if type(value) is not int or value != QUESTION_PROVENANCE_SCHEMA_VERSION:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_and_freeze_maps(self) -> QuestionProvenanceDocument:
        question_records = dict(self.by_question_id)
        legacy_records = dict(self.by_legacy_id)

        for question_id in question_records:
            validate_question_id(question_id, field="by_question_id key")
        for legacy_id in legacy_records:
            validate_legacy_question_id(legacy_id, field="by_legacy_id key")

        for legacy_record in legacy_records.values():
            current_id = legacy_record.current_question_id
            if current_id is None:
                continue
            question_record = question_records.get(current_id)
            if question_record is None or not _same_provenance(question_record, legacy_record):
                raise ValueError("legacy alias must reference an equal question provenance record")

        object.__setattr__(self, "by_question_id", MappingProxyType(question_records))
        object.__setattr__(self, "by_legacy_id", MappingProxyType(legacy_records))
        return self

    @field_serializer("by_question_id", "by_legacy_id")
    def serialize_mapping(self, value: Mapping[str, BaseModel]) -> dict[str, object]:
        return {key: record.model_dump(mode="json") for key, record in value.items()}


class QuestionProvenanceMutation(BaseModel):
    """Immutable result of an insertion, migration, or replay."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["inserted", "migrated", "replayed"]
    document: QuestionProvenanceDocument


def parse_question_provenance(data: bytes) -> QuestionProvenanceDocument:
    """Parse exact canonical schema-1 bytes into an immutable document."""
    if not isinstance(data, bytes):
        raise QuestionProvenanceMalformedJson("question provenance input must be bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QuestionProvenanceInvalidUtf8(
            "question provenance input must be valid UTF-8"
        ) from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except QuestionProvenanceError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise QuestionProvenanceMalformedJson(
            "question provenance input must be valid JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise QuestionProvenanceMalformedJson("question provenance root must be an object")

    version = raw.get("schema_version")
    if type(version) is int and version != QUESTION_PROVENANCE_SCHEMA_VERSION:
        raise QuestionProvenanceVersionUnsupported(
            "question provenance schema version is unsupported"
        )

    try:
        document = QuestionProvenanceDocument.model_validate(raw)
    except (ValidationError, InvalidQuestionIdentifier, InvalidLegacyQuestionIdentifier) as exc:
        raise QuestionProvenanceSchemaInvalid(
            "question provenance document violates schema 1"
        ) from exc

    if serialize_question_provenance(document) != data:
        raise QuestionProvenanceNoncanonical(
            "question provenance input is not canonical schema-1 JSON"
        )
    return document


def serialize_question_provenance(document: QuestionProvenanceDocument) -> bytes:
    """Serialize an immutable document to exact canonical schema-1 bytes."""
    if not isinstance(document, QuestionProvenanceDocument):
        raise QuestionProvenanceSchemaInvalid(
            "question provenance value must be a QuestionProvenanceDocument"
        )
    value = _document_value(document)
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def insert_question_provenance(
    document: QuestionProvenanceDocument,
    *,
    question_id: str,
    provenance: QuestionProvenanceRecord,
) -> QuestionProvenanceMutation:
    """Insert one canonical Q-form record, replay identically, or refuse conflict."""
    _require_document(document)
    _require_record(provenance)
    try:
        canonical_id = validate_question_id(question_id, field="question_id")
    except InvalidQuestionIdentifier as exc:
        raise QuestionProvenanceSchemaInvalid(
            "question_id must be a canonical unpadded Q-form"
        ) from exc

    existing = document.by_question_id.get(canonical_id)
    if existing is not None:
        if existing == provenance:
            return QuestionProvenanceMutation(status="replayed", document=document)
        raise QuestionProvenanceConflict("question provenance record already differs")

    question_records = dict(document.by_question_id)
    question_records[canonical_id] = provenance
    updated = QuestionProvenanceDocument(
        schema_version=QUESTION_PROVENANCE_SCHEMA_VERSION,
        by_question_id=question_records,
        by_legacy_id=document.by_legacy_id,
    )
    return QuestionProvenanceMutation(status="inserted", document=updated)


def migrate_legacy_question_provenance(
    document: QuestionProvenanceDocument,
    *,
    legacy_id: str,
    current_question_id: str,
) -> QuestionProvenanceMutation:
    """Bind retained legacy provenance to one derived canonical Q-form record."""
    _require_document(document)
    try:
        validated_legacy_id = validate_legacy_question_id(legacy_id)
        canonical_id = validate_question_id(current_question_id, field="current_question_id")
    except (InvalidLegacyQuestionIdentifier, InvalidQuestionIdentifier) as exc:
        raise QuestionProvenanceSchemaInvalid(
            "legacy migration identifiers violate the provenance schema"
        ) from exc

    legacy = document.by_legacy_id.get(validated_legacy_id)
    if legacy is None:
        raise QuestionProvenanceLegacyMissing("legacy question provenance is absent")
    if legacy.current_question_id not in (None, canonical_id):
        raise QuestionProvenanceConflict("legacy provenance already names another question")

    derived = QuestionProvenanceRecord(
        actor_id=legacy.actor_id,
        created_at=legacy.created_at,
        event_id=legacy.event_id,
    )
    existing = document.by_question_id.get(canonical_id)
    if existing is not None and existing != derived:
        raise QuestionProvenanceConflict("canonical question provenance already differs")
    if existing == derived and legacy.current_question_id == canonical_id:
        return QuestionProvenanceMutation(status="replayed", document=document)

    question_records = dict(document.by_question_id)
    question_records[canonical_id] = derived
    legacy_records = dict(document.by_legacy_id)
    legacy_records[validated_legacy_id] = legacy.model_copy(
        update={"current_question_id": canonical_id}
    )
    updated = QuestionProvenanceDocument(
        schema_version=QUESTION_PROVENANCE_SCHEMA_VERSION,
        by_question_id=question_records,
        by_legacy_id=legacy_records,
    )
    return QuestionProvenanceMutation(status="migrated", document=updated)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QuestionProvenanceDuplicateKey(
                "question provenance input contains a duplicate object key"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise QuestionProvenanceMalformedJson(
        "question provenance input contains an invalid JSON number"
    )


def _same_provenance(
    question: QuestionProvenanceRecord,
    legacy: LegacyQuestionProvenanceRecord,
) -> bool:
    return (
        question.actor_id == legacy.actor_id
        and question.created_at == legacy.created_at
        and question.event_id == legacy.event_id
    )


def _record_value(record: QuestionProvenanceRecord) -> dict[str, str]:
    return {
        "actor_id": record.actor_id,
        "created_at": record.created_at,
        "event_id": record.event_id,
    }


def _document_value(document: QuestionProvenanceDocument) -> dict[str, object]:
    legacy_values: dict[str, object] = {}
    for legacy_id, record in document.by_legacy_id.items():
        value: dict[str, object] = _record_value(record)
        value["current_question_id"] = record.current_question_id
        legacy_values[legacy_id] = value
    return {
        "schema_version": document.schema_version,
        "by_question_id": {
            question_id: _record_value(record)
            for question_id, record in document.by_question_id.items()
        },
        "by_legacy_id": legacy_values,
    }


def _require_document(document: object) -> QuestionProvenanceDocument:
    if not isinstance(document, QuestionProvenanceDocument):
        raise QuestionProvenanceSchemaInvalid("document must be a QuestionProvenanceDocument")
    return document


def _require_record(provenance: object) -> QuestionProvenanceRecord:
    if not isinstance(provenance, QuestionProvenanceRecord) or isinstance(
        provenance, LegacyQuestionProvenanceRecord
    ):
        raise QuestionProvenanceSchemaInvalid("provenance must be a QuestionProvenanceRecord")
    return provenance
