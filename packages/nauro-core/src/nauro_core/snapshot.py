"""Canonical snapshot serialization — pure compute, no I/O, no clock.

Both the local CLI capture path and the remote capture path build their
snapshot dicts here so the two on-disk formats cannot drift. The caller
supplies the timestamp (already an ISO string) and the file contents; the
serializer stamps the schema version, derives the token count, and emits
the keys in the canonical order.

The module is deliberately side-effect free: no filesystem access, no
``datetime.now`` call, no regex. That keeps it usable from any transport
and makes the output a pure function of its inputs.
"""

from __future__ import annotations

from typing import TypedDict

from nauro_core.constants import (
    CHARS_PER_TOKEN,
    LEGACY_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)


# Snapshot shape. These annotate the existing dict literals only; the dict is a
# plain dict at runtime and the key order is unchanged, so the on-disk byte
# order is preserved. Module-private (not in ``__all__``). ``version`` is a
# conditional key, so it lives on a ``total=False`` subclass (3.10 target: mixed
# totality, not ``NotRequired``).
class SnapshotBase(TypedDict):
    """The always-present keys of a serialized snapshot."""

    schema_version: int
    timestamp: str
    trigger: str
    trigger_detail: str
    token_count: int
    files: dict[str, str]


class Snapshot(SnapshotBase, total=False):
    """A snapshot; ``version`` is present only when the caller versions it."""

    version: int


def serialize_snapshot(
    *,
    timestamp: str,
    trigger: str,
    files: dict[str, str],
    trigger_detail: str = "",
    version: int | None = None,
) -> Snapshot:
    """Build a canonical snapshot dict, keyed in the order ``schema_version, version,
    timestamp, trigger, trigger_detail, token_count, files``, with ``version`` present
    only when supplied. Raises ``ValueError`` on a bad type; the caller owns the clock.
    """
    if not isinstance(files, dict):
        raise ValueError("files must be a dict of filename to content.")
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError("timestamp must be a non-empty string.")
    if not isinstance(trigger, str):
        raise ValueError("trigger must be a string.")

    token_count = sum(len(v) for v in files.values()) // CHARS_PER_TOKEN

    snapshot: dict = {"schema_version": SNAPSHOT_SCHEMA_VERSION}
    if version is not None:
        snapshot["version"] = version
    snapshot["timestamp"] = timestamp
    snapshot["trigger"] = trigger
    snapshot["trigger_detail"] = trigger_detail
    snapshot["token_count"] = token_count
    snapshot["files"] = files
    return snapshot


def normalize_snapshot(raw: dict) -> Snapshot:
    """Fill in defaults a legacy snapshot may omit: ``schema_version`` to
    ``LEGACY_SCHEMA_VERSION``, ``trigger`` and ``trigger_detail`` to empty strings,
    ``token_count`` to ``0``, ``files`` to ``{}``. ``version`` survives if present.
    """
    normalized: dict = {
        "schema_version": raw.get("schema_version", LEGACY_SCHEMA_VERSION),
    }
    if "version" in raw:
        normalized["version"] = raw["version"]
    normalized["timestamp"] = raw.get("timestamp", "")
    normalized["trigger"] = raw.get("trigger", "")
    normalized["trigger_detail"] = raw.get("trigger_detail", "")
    normalized["token_count"] = raw.get("token_count", 0)
    normalized["files"] = raw.get("files", {})
    return normalized


def snapshot_schema_version(raw: dict) -> int:
    """Return a snapshot's schema version, defaulting legacy reads to 0."""
    return raw.get("schema_version", LEGACY_SCHEMA_VERSION)
