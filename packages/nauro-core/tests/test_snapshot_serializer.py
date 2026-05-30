"""Tests for the canonical snapshot serializer.

The serializer is pure compute: no I/O, no clock, no regex. These tests
pin the on-disk dict shape (key order, schema stamping, token derivation)
and the legacy read-path normalization both surfaces rely on.
"""

from __future__ import annotations

import pytest

from nauro_core.snapshot import (
    normalize_snapshot,
    serialize_snapshot,
    snapshot_schema_version,
)

# 12 + 8 = 20 characters across the two files; 20 // 4 == 5.
_FILES = {"state_current.md": "# State\nabcd", "stack.md": "# Stack\n"}
_TIMESTAMP = "2026-05-29T12:00:00+00:00"


def test_serialize_stamps_schema_and_derives_token_count() -> None:
    snap = serialize_snapshot(timestamp=_TIMESTAMP, trigger="sync", files=_FILES)
    assert snap["schema_version"] == 1
    assert snap["trigger"] == "sync"
    assert snap["trigger_detail"] == ""
    assert snap["token_count"] == 5
    assert snap["files"] == _FILES


def test_serialize_omits_version_when_not_supplied() -> None:
    snap = serialize_snapshot(timestamp=_TIMESTAMP, trigger="sync", files=_FILES)
    assert "version" not in snap


def test_serialize_includes_version_when_supplied() -> None:
    snap = serialize_snapshot(timestamp=_TIMESTAMP, trigger="sync", files=_FILES, version=7)
    assert snap["version"] == 7


def test_serialize_local_key_order_is_canonical() -> None:
    snap = serialize_snapshot(
        timestamp=_TIMESTAMP,
        trigger="sync",
        trigger_detail="detail",
        files=_FILES,
        version=7,
    )
    assert list(snap.keys()) == [
        "schema_version",
        "version",
        "timestamp",
        "trigger",
        "trigger_detail",
        "token_count",
        "files",
    ]


def test_serialize_versionless_key_order_omits_version() -> None:
    snap = serialize_snapshot(timestamp=_TIMESTAMP, trigger="sync", files=_FILES)
    assert list(snap.keys()) == [
        "schema_version",
        "timestamp",
        "trigger",
        "trigger_detail",
        "token_count",
        "files",
    ]


def test_serialize_rejects_non_dict_files() -> None:
    with pytest.raises(ValueError):
        serialize_snapshot(timestamp=_TIMESTAMP, trigger="sync", files=["not", "a", "dict"])


def test_serialize_rejects_empty_timestamp() -> None:
    with pytest.raises(ValueError):
        serialize_snapshot(timestamp="", trigger="sync", files=_FILES)


def test_serialize_rejects_non_str_timestamp() -> None:
    with pytest.raises(ValueError):
        serialize_snapshot(timestamp=1234567890, trigger="sync", files=_FILES)


def test_serialize_is_clock_free_and_deterministic() -> None:
    first = serialize_snapshot(timestamp=_TIMESTAMP, trigger="sync", files=_FILES, version=3)
    second = serialize_snapshot(timestamp=_TIMESTAMP, trigger="sync", files=_FILES, version=3)
    assert first == second


def test_normalize_defaults_absent_schema_version_to_legacy() -> None:
    normalized = normalize_snapshot({"timestamp": _TIMESTAMP, "files": {}})
    assert normalized["schema_version"] == 0
    assert normalized["trigger_detail"] == ""
    assert normalized["token_count"] == 0
    assert normalized["files"] == {}


def test_normalize_preserves_present_schema_version() -> None:
    normalized = normalize_snapshot({"schema_version": 1, "timestamp": _TIMESTAMP, "files": {}})
    assert normalized["schema_version"] == 1


def test_normalize_round_trips_version_when_present() -> None:
    normalized = normalize_snapshot({"version": 9, "timestamp": _TIMESTAMP, "files": {}})
    assert normalized["version"] == 9


def test_normalize_omits_version_when_absent() -> None:
    normalized = normalize_snapshot({"timestamp": _TIMESTAMP, "files": {}})
    assert "version" not in normalized


def test_snapshot_schema_version_defaults_absent_to_legacy() -> None:
    assert snapshot_schema_version({"timestamp": _TIMESTAMP}) == 0


def test_snapshot_schema_version_reads_present_value() -> None:
    assert snapshot_schema_version({"schema_version": 1}) == 1
