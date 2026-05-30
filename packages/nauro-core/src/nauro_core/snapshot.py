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

from nauro_core.constants import (
    CHARS_PER_TOKEN,
    LEGACY_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)


def serialize_snapshot(
    *,
    timestamp: str,
    trigger: str,
    files: dict[str, str],
    trigger_detail: str = "",
    version: int | None = None,
) -> dict:
    """Build a canonical snapshot dict from its parts.

    Args:
        timestamp: ISO-8601 timestamp string. The caller owns the clock;
            the serializer never calls ``datetime.now``.
        trigger: Short description of what triggered the snapshot.
        files: Mapping of store-relative filename to file content.
        trigger_detail: Optional extra detail about the trigger.
        version: Dense snapshot version. Local supplies its integer here;
            callers that do not version snapshots omit it, and the
            ``version`` key is then left out of the emitted dict entirely.

    Returns:
        A snapshot dict whose keys appear in the canonical order
        ``schema_version, version, timestamp, trigger, trigger_detail,
        token_count, files``. ``version`` is present only when supplied.

    Raises:
        ValueError: If ``files`` is not a dict, ``timestamp`` is empty or
            not a string, or ``trigger`` is not a string. The timestamp
            *format* is not validated — surfaces already produce ISO.
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


def normalize_snapshot(raw: dict) -> dict:
    """Fill in defaults for fields a legacy snapshot may omit.

    Legacy snapshots predate ``schema_version`` and may lack
    ``trigger_detail`` / ``token_count``. This read-path helper returns a
    dict with those fields defaulted so consumers that distinguish on
    ``schema_version`` / ``version`` can read old and new snapshots
    uniformly. ``version`` round-trips only when present — it is never
    required.

    Args:
        raw: A snapshot dict as read back from storage.

    Returns:
        A new dict with ``schema_version`` defaulted to
        ``LEGACY_SCHEMA_VERSION`` when absent, ``trigger`` / ``trigger_detail``
        to ``""``, ``token_count`` to ``0``, and ``files`` to ``{}``.
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
