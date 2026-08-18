"""Tests for the immutable question-provenance sidecar codec."""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from nauro_core.question_provenance import (
    LegacyQuestionProvenanceRecord,
    QuestionProvenanceConflict,
    QuestionProvenanceDocument,
    QuestionProvenanceDuplicateKey,
    QuestionProvenanceInvalidUtf8,
    QuestionProvenanceMalformedJson,
    QuestionProvenanceMutation,
    QuestionProvenanceNoncanonical,
    QuestionProvenanceRecord,
    QuestionProvenanceSchemaInvalid,
    QuestionProvenanceVersionUnsupported,
    insert_question_provenance,
    migrate_legacy_question_provenance,
    parse_question_provenance,
    serialize_question_provenance,
)
from nauro_core.questions import OpenQuestionsFile

ACTOR_ID = "0" + "A" * 25
EVENT_ID = "1" + "B" * 25
CREATED_AT = "2026-08-18T00:01:02.000003Z"
LEGACY_ID = "2026-05-12 20:18 UTC"


def _record() -> QuestionProvenanceRecord:
    return QuestionProvenanceRecord(
        actor_id=ACTOR_ID,
        created_at=CREATED_AT,
        event_id=EVENT_ID,
    )


def _legacy(*, current_question_id: str | None = None) -> LegacyQuestionProvenanceRecord:
    return LegacyQuestionProvenanceRecord(
        actor_id=ACTOR_ID,
        created_at=CREATED_AT,
        event_id=EVENT_ID,
        current_question_id=current_question_id,
    )


def _empty_document() -> QuestionProvenanceDocument:
    return QuestionProvenanceDocument(schema_version=1, by_question_id={}, by_legacy_id={})


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        + b"\n"
    )


class TestCanonicalCodec:
    def test_empty_document_has_exact_canonical_bytes(self):
        document = _empty_document()
        assert serialize_question_provenance(document) == (
            b'{"by_legacy_id":{},"by_question_id":{},"schema_version":1}\n'
        )

    def test_nested_maps_and_fields_are_sorted_with_raw_unicode(self):
        actor = "0" + "C" * 25
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={
                "Q17": QuestionProvenanceRecord(
                    actor_id=actor,
                    created_at=CREATED_AT,
                    event_id=EVENT_ID,
                ),
                "Q2": _record(),
            },
            by_legacy_id={},
        )
        encoded = serialize_question_provenance(document)
        assert encoded.index(b'"Q17"') < encoded.index(b'"Q2"')
        assert b": " not in encoded
        assert encoded.endswith(b"\n")
        assert not encoded.endswith(b"\n\n")

    def test_migrated_alias_has_exact_canonical_bytes(self):
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={"Q1": _record()},
            by_legacy_id={LEGACY_ID: _legacy(current_question_id="Q1")},
        )
        assert serialize_question_provenance(document) == (
            b'{"by_legacy_id":{"2026-05-12 20:18 UTC":{"actor_id":"'
            + ACTOR_ID.encode()
            + b'","created_at":"2026-08-18T00:01:02.000003Z",'
            + b'"current_question_id":"Q1","event_id":"'
            + EVENT_ID.encode()
            + b'"}},"by_question_id":{"Q1":{"actor_id":"'
            + ACTOR_ID.encode()
            + b'","created_at":"2026-08-18T00:01:02.000003Z","event_id":"'
            + EVENT_ID.encode()
            + b'"}},"schema_version":1}\n'
        )

    def test_parse_and_serialize_are_deterministic(self):
        data = _canonical(
            {
                "schema_version": 1,
                "by_question_id": {
                    "Q1": {
                        "actor_id": ACTOR_ID,
                        "created_at": CREATED_AT,
                        "event_id": EVENT_ID,
                    }
                },
                "by_legacy_id": {},
            }
        )
        first = parse_question_provenance(data)
        second = parse_question_provenance(serialize_question_provenance(first))
        assert first == second
        assert serialize_question_provenance(second) == data

    @pytest.mark.parametrize(
        "data",
        [
            b'{"by_legacy_id": {},"by_question_id":{},"schema_version":1}\n',
            b'{"schema_version":1,"by_question_id":{},"by_legacy_id":{}}\n',
            b'{"by_legacy_id":{},"by_question_id":{},"schema_version":1}',
            b'{"by_legacy_id":{},"by_question_id":{},"schema_version":1}\n\n',
        ],
    )
    def test_semantically_valid_noncanonical_bytes_reject(self, data):
        with pytest.raises(QuestionProvenanceNoncanonical) as exc_info:
            parse_question_provenance(data)
        assert exc_info.value.classification == "question_provenance_noncanonical"


class TestParseFailures:
    def test_invalid_utf8_has_closed_classification(self):
        with pytest.raises(QuestionProvenanceInvalidUtf8) as exc_info:
            parse_question_provenance(b"\xff")
        assert exc_info.value.classification == "question_provenance_invalid_utf8"

    @pytest.mark.parametrize("data", [b"{\n", b"[]\n", b'{"schema_version":NaN}\n'])
    def test_malformed_json_or_root_rejects(self, data):
        with pytest.raises(QuestionProvenanceMalformedJson):
            parse_question_provenance(data)

    def test_oversized_json_integer_has_malformed_classification(self):
        data = b'{"schema_version":' + b"1" * 5000 + b"}\n"
        with pytest.raises(QuestionProvenanceMalformedJson) as exc_info:
            parse_question_provenance(data)
        assert exc_info.value.classification == "question_provenance_json_malformed"

    def test_json_recursion_error_has_malformed_classification(self):
        with (
            patch("nauro_core.question_provenance.json.loads", side_effect=RecursionError),
            pytest.raises(QuestionProvenanceMalformedJson) as exc_info,
        ):
            parse_question_provenance(b"{}\n")
        assert exc_info.value.classification == "question_provenance_json_malformed"

    def test_parsed_nested_wrong_shape_has_schema_classification(self):
        data = _canonical({"schema_version": 1, "by_question_id": [[0]], "by_legacy_id": {}})
        with pytest.raises(QuestionProvenanceSchemaInvalid) as exc_info:
            parse_question_provenance(data)
        assert exc_info.value.classification == "question_provenance_schema_invalid"

    @pytest.mark.parametrize(
        "data",
        [
            b'{"schema_version":1,"schema_version":1,"by_question_id":{},"by_legacy_id":{}}\n',
            b'{"schema_version":1,"by_question_id":{"Q1":{},"Q1":{}},"by_legacy_id":{}}\n',
            (
                b'{"schema_version":1,"by_question_id":{},"by_legacy_id":'
                b'{"2026-05-12 20:18 UTC":{},"2026-05-12 20:18 UTC":{}}}\n'
            ),
            b'{"schema_version":1,"by_question_id":{"Q1":{"actor_id":"x","actor_id":"x","created_at":"x","event_id":"x"}},"by_legacy_id":{}}\n',
        ],
    )
    def test_duplicate_keys_reject_at_every_object_level(self, data):
        with pytest.raises(QuestionProvenanceDuplicateKey) as exc_info:
            parse_question_provenance(data)
        assert exc_info.value.classification == "question_provenance_duplicate_key"

    def test_unsupported_version_has_exact_classification(self):
        data = _canonical({"schema_version": 2, "by_question_id": {}, "by_legacy_id": {}})
        with pytest.raises(QuestionProvenanceVersionUnsupported) as exc_info:
            parse_question_provenance(data)
        assert exc_info.value.classification == "question_provenance_version_unsupported"

    @pytest.mark.parametrize(
        "value",
        [
            {"schema_version": True, "by_question_id": {}, "by_legacy_id": {}},
            {
                "schema_version": 1,
                "by_question_id": {},
                "by_legacy_id": {},
                "extra": None,
            },
            {"schema_version": 1, "by_question_id": {}},
            {"schema_version": 1, "by_question_id": [], "by_legacy_id": {}},
            {
                "schema_version": 1,
                "by_question_id": {
                    "Q1": {
                        "actor_id": ACTOR_ID,
                        "created_at": CREATED_AT,
                        "event_id": EVENT_ID,
                        "extra": "x",
                    }
                },
                "by_legacy_id": {},
            },
        ],
    )
    def test_closed_schema_rejects_unknown_missing_and_wrong_fields(self, value):
        with pytest.raises(QuestionProvenanceSchemaInvalid) as exc_info:
            parse_question_provenance(_canonical(value))
        assert exc_info.value.classification == "question_provenance_schema_invalid"

    @pytest.mark.parametrize(
        ("question_id", "actor_id", "created_at", "event_id"),
        [
            ("Q01", ACTOR_ID, CREATED_AT, EVENT_ID),
            ("Q1", "8" + "A" * 25, CREATED_AT, EVENT_ID),
            ("Q1", ACTOR_ID, "2026-08-18T00:01:02Z", EVENT_ID),
            ("Q1", ACTOR_ID, CREATED_AT, "I" * 26),
        ],
    )
    def test_shared_validators_reject_invalid_q_actor_time_and_event(
        self, question_id, actor_id, created_at, event_id
    ):
        value = {
            "schema_version": 1,
            "by_question_id": {
                question_id: {
                    "actor_id": actor_id,
                    "created_at": created_at,
                    "event_id": event_id,
                }
            },
            "by_legacy_id": {},
        }
        with pytest.raises(QuestionProvenanceSchemaInvalid):
            parse_question_provenance(_canonical(value))

    @pytest.mark.parametrize(
        ("legacy_id", "current_question_id"),
        [("2026-5-12 20:18 UTC", None), (LEGACY_ID, "Q01")],
    )
    def test_legacy_keys_and_alias_targets_use_shared_validators(
        self, legacy_id, current_question_id
    ):
        value = {
            "schema_version": 1,
            "by_question_id": {},
            "by_legacy_id": {
                legacy_id: {
                    "actor_id": ACTOR_ID,
                    "created_at": CREATED_AT,
                    "event_id": EVENT_ID,
                    "current_question_id": current_question_id,
                }
            },
        }
        with pytest.raises(QuestionProvenanceSchemaInvalid):
            parse_question_provenance(_canonical(value))

    def test_alias_requires_matching_question_record(self):
        value = {
            "schema_version": 1,
            "by_question_id": {},
            "by_legacy_id": {
                LEGACY_ID: {
                    "actor_id": ACTOR_ID,
                    "created_at": CREATED_AT,
                    "event_id": EVENT_ID,
                    "current_question_id": "Q1",
                }
            },
        }
        with pytest.raises(QuestionProvenanceSchemaInvalid):
            parse_question_provenance(_canonical(value))

    def test_alias_requires_equal_question_provenance(self):
        value = {
            "schema_version": 1,
            "by_question_id": {
                "Q1": {
                    "actor_id": "0" + "C" * 25,
                    "created_at": CREATED_AT,
                    "event_id": EVENT_ID,
                }
            },
            "by_legacy_id": {
                LEGACY_ID: {
                    "actor_id": ACTOR_ID,
                    "created_at": CREATED_AT,
                    "event_id": EVENT_ID,
                    "current_question_id": "Q1",
                }
            },
        }
        with pytest.raises(QuestionProvenanceSchemaInvalid):
            parse_question_provenance(_canonical(value))


class TestImmutability:
    def test_models_and_nested_mappings_are_immutable_and_copied(self):
        records = {"Q1": _record()}
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id=records,
            by_legacy_id={},
        )
        records.clear()
        assert tuple(document.by_question_id) == ("Q1",)
        with pytest.raises(TypeError):
            document.by_question_id["Q2"] = _record()  # type: ignore[index]
        with pytest.raises(ValidationError):
            document.schema_version = 2  # type: ignore[misc]
        with pytest.raises(ValidationError):
            document.by_question_id["Q1"].actor_id = ACTOR_ID  # type: ignore[misc]

    def test_model_dump_serializes_frozen_maps(self):
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={"Q1": _record()},
            by_legacy_id={},
        )
        assert document.model_dump(mode="json") == {
            "schema_version": 1,
            "by_question_id": {
                "Q1": {
                    "actor_id": ACTOR_ID,
                    "created_at": CREATED_AT,
                    "event_id": EVENT_ID,
                }
            },
            "by_legacy_id": {},
        }


class TestInsertion:
    def test_insert_then_exact_replay(self):
        inserted = insert_question_provenance(
            _empty_document(), question_id="Q1", provenance=_record()
        )
        assert inserted == QuestionProvenanceMutation(
            status="inserted",
            document=inserted.document,
        )
        assert inserted.document.by_question_id["Q1"] == _record()

        replayed = insert_question_provenance(
            inserted.document, question_id="Q1", provenance=_record()
        )
        assert replayed.status == "replayed"
        assert replayed.document is inserted.document

    def test_padded_provenance_key_rejects(self):
        with pytest.raises(QuestionProvenanceSchemaInvalid):
            insert_question_provenance(_empty_document(), question_id="Q01", provenance=_record())

    def test_divergent_duplicate_refuses(self):
        inserted = insert_question_provenance(
            _empty_document(), question_id="Q1", provenance=_record()
        )
        divergent = QuestionProvenanceRecord(
            actor_id="0" + "C" * 25,
            created_at=CREATED_AT,
            event_id=EVENT_ID,
        )
        with pytest.raises(QuestionProvenanceConflict) as exc_info:
            insert_question_provenance(inserted.document, question_id="Q1", provenance=divergent)
        assert exc_info.value.classification == "question_provenance_conflict"


class TestLegacyMigration:
    def test_migration_derives_record_and_retains_alias(self):
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={},
            by_legacy_id={LEGACY_ID: _legacy()},
        )
        result = migrate_legacy_question_provenance(
            document, legacy_id=LEGACY_ID, current_question_id="Q1"
        )
        assert result.status == "migrated"
        assert result.document.by_question_id["Q1"] == _record()
        assert result.document.by_legacy_id[LEGACY_ID] == _legacy(current_question_id="Q1")

        replayed = migrate_legacy_question_provenance(
            result.document, legacy_id=LEGACY_ID, current_question_id="Q1"
        )
        assert replayed.status == "replayed"
        assert replayed.document is result.document

    def test_migration_completes_alias_when_equal_question_record_exists(self):
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={"Q1": _record()},
            by_legacy_id={LEGACY_ID: _legacy()},
        )
        result = migrate_legacy_question_provenance(
            document, legacy_id=LEGACY_ID, current_question_id="Q1"
        )
        assert result.status == "migrated"
        assert result.document.by_legacy_id[LEGACY_ID].current_question_id == "Q1"

    def test_migration_rejects_missing_legacy_record(self):
        with pytest.raises(QuestionProvenanceConflict) as exc_info:
            migrate_legacy_question_provenance(
                _empty_document(), legacy_id=LEGACY_ID, current_question_id="Q1"
            )
        assert exc_info.value.classification == "question_provenance_legacy_missing"

    def test_migration_rejects_different_existing_alias(self):
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={"Q2": _record()},
            by_legacy_id={LEGACY_ID: _legacy(current_question_id="Q2")},
        )
        with pytest.raises(QuestionProvenanceConflict):
            migrate_legacy_question_provenance(
                document, legacy_id=LEGACY_ID, current_question_id="Q1"
            )

    def test_migration_rejects_divergent_existing_question_record(self):
        divergent = QuestionProvenanceRecord(
            actor_id="0" + "C" * 25,
            created_at=CREATED_AT,
            event_id=EVENT_ID,
        )
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={"Q1": divergent},
            by_legacy_id={LEGACY_ID: _legacy()},
        )
        with pytest.raises(QuestionProvenanceConflict):
            migrate_legacy_question_provenance(
                document, legacy_id=LEGACY_ID, current_question_id="Q1"
            )


class TestPreservationAndIsolation:
    def test_resolution_does_not_change_provenance_bytes(self):
        document = QuestionProvenanceDocument(
            schema_version=1,
            by_question_id={"Q1": _record()},
            by_legacy_id={},
        )
        before = serialize_question_provenance(document)
        questions = OpenQuestionsFile.parse("# Open Questions\n\n- [Q1] one\n")
        questions.resolve(["Q1"], 7, date(2026, 8, 18))
        assert serialize_question_provenance(document) == before

    def test_codec_has_no_storage_or_network_imports(self):
        source_path = Path(__file__).parents[1] / "src/nauro_core/question_provenance.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        forbidden = {"boto3", "botocore", "httpx", "requests", "socket", "urllib"}
        roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert roots.isdisjoint(forbidden)

    def test_nauro_package_has_no_production_consumer(self):
        package_root = Path(__file__).parents[2] / "nauro/src"
        consumers: list[str] = []
        for path in package_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == (
                    "nauro_core.question_provenance"
                ):
                    consumers.append(str(path.relative_to(package_root)))
                if isinstance(node, ast.Import) and any(
                    alias.name == "nauro_core.question_provenance" for alias in node.names
                ):
                    consumers.append(str(path.relative_to(package_root)))
        assert consumers == []
