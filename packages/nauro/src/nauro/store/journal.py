"""Write-path provenance journal: append-only, store-local, journal-only.

Every write-path action (a decision proposal, a question flag, a state update)
appends one origin-stamped event to ``journal/events.jsonl`` under the project
store. The journal is store data, not telemetry: it records which surface
originated a write. Capture only in 1.x: no viewer, no rotation, no cloud sync.
The file is one ASCII-escaped JSON event per line, and a crash-truncated final
record is closed before the next append.

Events are attempt markers, not deltas: ``payload_hash`` proves two payloads
differed, it does not preserve a proposed-vs-ratified delta. Author identity is
not captured; the ``actor`` slot is reserved and left unset.

Fail-open is a hard invariant: :func:`append_event` never raises, so a
journaling failure never blocks the underlying store write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger("nauro.store.journal")

# Journal layout under the store root. Non-public, internal to this surface:
# these names must never enter the frozen public-contract constant set or
# nauro_core.__all__.
JOURNAL_DIR = "journal"
JOURNAL_EVENTS_FILENAME = "events.jsonl"
# Dedicated lock, distinct from every resource lock (decisions/.lock,
# <file>.rmwlock). It lives inside journal/, so the sync journal/ prefix rule
# already excludes it from cloud sync.
JOURNAL_LOCK_NAME = ".lock"

# Bound for client-supplied origin strings. MCP ``initialize`` metadata is
# client-supplied and unauthenticated, so both fields are length-bounded and
# stripped of control characters before they are recorded.
_MAX_ORIGIN_FIELD_LEN = 256


def _sanitize_client_string(value: str) -> str:
    """Strip control characters and bound the length of a client-supplied string."""
    cleaned = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)
    return cleaned[:_MAX_ORIGIN_FIELD_LEN]


def _utc_now_rfc3339() -> str:
    """Current UTC time as an RFC3339 timestamp."""
    return datetime.now(timezone.utc).isoformat()


class OriginDescriptor(BaseModel):
    """Where a write-path action originated.

    ``transport`` is the only enumerated axis; ``client_name`` and
    ``client_version`` are free-form recorded values with no vendor enumeration.
    ``actor`` is a reserved slot for a future author-identity axis, left unset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["cli", "stdio-mcp", "hosted-mcp"]
    client_name: str | None = None
    client_version: str | None = None
    actor: str | None = None

    @field_validator("client_name", "client_version", "actor")
    @classmethod
    def _bound_and_sanitize(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _sanitize_client_string(value)


class JournalEvent(BaseModel):
    """One append-only write-path provenance record.

    ``status`` is ``committed`` when the underlying write executed and ``rejected``
    when the action was refused. ``decision_id`` appears only on a committed
    decision write, and ``payload_hash`` is an attempt marker over the payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_version: int = 1
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=_utc_now_rfc3339)
    event_type: str = "write"
    operation: str
    target: str
    status: Literal["committed", "rejected"]
    decision_id: str | None = None
    origin: OriginDescriptor | None = None
    payload_hash: str
    hash_algorithm: str = "sha256"
    serialization: str = "json-sorted-compact-utf8"


def payload_hash(payload: dict) -> str:
    """Return the sha256 hex digest of ``payload`` under a canonical serialization.

    JSON with sorted keys and compact separators, so dict ordering cannot change it.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_event(
    store_path: Path,
    *,
    operation: str,
    target: str,
    status: str,
    payload: dict,
    origin_factory: Callable[[], OriginDescriptor | None] | None = None,
    decision_id: str | None = None,
) -> None:
    """Construct, hash, and append a write-path event. Fully fail-open.
    ``origin_factory`` is called inside the guard rather than passed as a built
    descriptor, so a raising builder, an unhashable payload or a bad status cannot escape.
    """
    try:
        origin = origin_factory() if origin_factory is not None else None
        event = JournalEvent(
            operation=operation,
            target=target,
            status=status,  # type: ignore[arg-type]
            decision_id=decision_id,
            origin=origin,
            payload_hash=payload_hash(payload),
        )
        append_event(store_path, event)
    except Exception:
        logger.debug("journal event emission failed for %s", operation, exc_info=True)


def append_event(store_path: Path, event: JournalEvent) -> None:
    """Append one already-built event to the store's journal. Never raises.

    The journal lock is taken only after the caller's resource lock is released.
    """
    try:
        journal_dir = store_path / JOURNAL_DIR
        journal_dir.mkdir(parents=True, exist_ok=True)
        lock_path = journal_dir / JOURNAL_LOCK_NAME
        line = (
            json.dumps(event.model_dump(mode="json", exclude_none=True), ensure_ascii=True) + "\n"
        )
        events_path = journal_dir / JOURNAL_EVENTS_FILENAME
        with FileLock(str(lock_path)):
            with events_path.open("a+b") as fh:
                if fh.seek(0, os.SEEK_END) > 0:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        fh.write(b"\n")
                fh.write(line.encode("utf-8"))
    except Exception:
        logger.debug("journal append failed for %s", store_path, exc_info=True)


def read_events(store_path: Path) -> list[JournalEvent]:
    """Read all parseable events from the store's journal.

    Internal reader: a line that fails to decode, parse, or validate is skipped.
    """
    events_file = store_path / JOURNAL_DIR / JOURNAL_EVENTS_FILENAME
    if not events_file.exists():
        return []
    events: list[JournalEvent] = []
    for chunk in events_file.read_bytes().split(b"\n"):
        if not chunk.strip():
            continue
        try:
            events.append(JournalEvent.model_validate(json.loads(chunk.decode("utf-8"))))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            continue
    return events
