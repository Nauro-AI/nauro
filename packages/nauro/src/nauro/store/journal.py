"""Write-path provenance journal — append-only, store-local, journal-only.

Every write-path action (a decision proposal, a question flag, a state update)
appends one origin-stamped event to ``journal/events.jsonl`` under the project
store. The journal is store data, not telemetry: it records *which surface*
originated a write so the provenance axis is auditable later. It is deliberately
narrow in 1.x — capture only, no viewer, no rotation, no cloud sync, no snapshot
capture.

Attempt markers, not deltas: the recorded ``payload_hash`` proves two payloads
differed; it does not preserve a proposed-vs-ratified delta.

Author identity is not captured. The origin descriptor reserves an ``actor``
slot additively, but pre-team stores are single-owner so that axis stays
recoverable, and team-identity capture is barred until retention is proven.

Fail-open is a hard invariant: :func:`append_event` never raises. A journaling
failure must never fail or block the underlying store write.
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
    ``client_version`` are free-form recorded values with no vendor
    enumeration. ``actor`` is a reserved slot for a future author-identity
    axis — it is defined additively but left unset, because team-identity
    capture is barred until retention is proven.
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

    ``status`` is ``committed`` when the underlying write executed and
    ``rejected`` when the action was attempted but refused (a Tier-1 or kernel
    rejection). ``decision_id`` is present only on a committed decision write.
    ``payload_hash`` is an attempt marker over a canonical serialization of the
    action payload, named by ``hash_algorithm`` / ``serialization``.
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

    The serialization is JSON with sorted keys and compact separators, encoded
    UTF-8. It is key-order independent by construction, so two payloads that
    differ only in dict ordering hash identically.
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
    """Construct, hash, and append a write-path event — fully fail-open.

    The origin is built by ``origin_factory`` *inside* the guard rather than
    passed as a value: an argument expression is evaluated before the call, so
    passing a constructed descriptor would leave its construction outside the
    fail-open region. Hashing, origin construction, and event construction all
    join the append inside one ``try``: a journaling defect (an unhashable
    payload, a bad status, a raising origin builder) must never escape into the
    caller after the underlying store write has already committed.
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

    A journaling failure must never fail or block the underlying store write,
    so every error here is swallowed with a debug log. The dedicated journal
    lock is acquired after the caller's resource lock has been released, so it
    never nests inside a store or decision write lock.

    The line is serialized with ``ensure_ascii=True`` so no raw non-ASCII byte
    (including the Unicode line separators U+0085/U+2028/U+2029) can ever land
    in the file and corrupt the one-event-per-line framing.

    A crash mid-append can leave a final record with no trailing newline;
    before appending, the file is checked and a newline is inserted first if
    needed, so a new committed event never concatenates onto a corrupt partial
    line and drag both into unreadability.

    Rotation is deliberately deferred: the journal grows unbounded in 1.x.
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

    Internal reader — not wired to any CLI or MCP surface. The file is read as
    bytes and split on ``b"\\n"`` so a truncated final record — an interrupted
    append can leave a partial multibyte UTF-8 character — is decoded per line
    inside the tolerated path: any line that fails to decode, parse, or validate
    is skipped rather than raising.
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
