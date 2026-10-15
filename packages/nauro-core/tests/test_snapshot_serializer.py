"""Tests for the canonical snapshot serializer.

The serializer is pure compute: no I/O, no clock, no regex. These tests
pin the on-disk dict shape (key order, schema stamping, token derivation)
both surfaces rely on.
"""

from __future__ import annotations

import pytest
from nauro_core.snapshot import serialize_snapshot

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
