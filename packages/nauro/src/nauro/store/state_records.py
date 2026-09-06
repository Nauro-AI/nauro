"""Private durable state identities with recoverable no-write observations."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from filelock import BaseFileLock, FileLock, UnixFileLock, WindowsFileLock
from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.provenance import validate_utc_timestamp
from pydantic import Field, StrictInt, StrictStr, ValidationError, field_validator, model_validator

from nauro.store.home import nauro_home
from nauro.store.state_contract import (
    ClosedModel,
    StateResult,
    StateScope,
    read_state_payload,
    verify_state_response,
)
from nauro.store.submission_records import (
    SUBMISSION_RECORDS_DIR,
    Digest,
    SubmissionRecordCorruptError,
    SubmissionRecordError,
    _directory_sync,
    _ensure_directory,
    require_submission_actor,
)

_MAX_BYTES = 2 * 1024 * 1024


class StateSubmission(ClosedModel):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    scope: StateScope
    created_at: StrictStr
    payload_json: StrictStr
    payload_digest: Digest
    phase: Literal["prepared", "uncertain", "resolved"]
    result: Annotated[StateResult, Field(discriminator="status")] | None = None

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="created_at")

    @model_validator(mode="after")
    def _bindings(self) -> StateSubmission:
        read_state_payload(self.payload_json)
        if hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest() != self.payload_digest:
            raise ValueError("state payload digest differs")
        if self.result is not None:
            verify_state_response(
                self.result.model_dump_json().encode(), self.scope, self.payload_json
            )
        terminal = self.result is not None and not self.result.unresolved
        if (self.phase == "resolved") != terminal:
            raise ValueError("state phase and result disagree")
        if self.phase == "prepared" and self.result is not None:
            raise ValueError("prepared state cannot contain a response")
        return self


class _Stored(ClosedModel):
    record: StateSubmission
    digest: Digest

    @model_validator(mode="after")
    def _integrity(self) -> _Stored:
        if hashlib.sha256(self.record.model_dump_json().encode()).hexdigest() != self.digest:
            raise ValueError("state record checksum differs")
        return self


def _directory(project_id: str, user_id: str) -> Path:
    validate_identifier(IdentifierKind.ulid, project_id, field="project_id")
    validate_identifier(IdentifierKind.ulid, user_id, field="user_id")
    return nauro_home() / SUBMISSION_RECORDS_DIR / "state" / project_id / user_id


def _record_path(scope: StateScope) -> Path:
    scope = StateScope.model_validate(scope)
    key = hashlib.sha256(scope.model_dump_json().encode()).hexdigest()
    return _directory(scope.project_id, scope.user_id) / f"{key}.json"


def _read(path: Path) -> StateSubmission | None:
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SubmissionRecordCorruptError("The state record is not a regular file.")
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise SubmissionRecordCorruptError("The state record exceeds its size limit.")
        stored = _Stored.model_validate_json(raw, strict=True)
        if stored.model_dump_json().encode() != raw:
            raise SubmissionRecordCorruptError("The state record encoding differs.")
        return stored.record
    except (ValidationError, UnicodeError, SubmissionRecordError) as exc:
        raise SubmissionRecordCorruptError("The state record did not verify.") from exc


def read_state_submission(scope: StateScope) -> StateSubmission | None:
    require_submission_actor(scope.user_id)
    record = _read(_record_path(scope))
    if record is not None and record.scope != scope:
        raise SubmissionRecordCorruptError("The state record belongs to another scope.")
    require_submission_actor(scope.user_id)
    return record


def list_state_submissions(project_id: str, user_id: str) -> tuple[StateSubmission, ...]:
    require_submission_actor(user_id)
    try:
        entries = os.scandir(_directory(project_id, user_id))
    except FileNotFoundError:
        return ()
    records = []
    with entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                record = _read(Path(entry.path))
                if (
                    record is None
                    or record.scope.project_id != project_id
                    or record.scope.user_id != user_id
                    or _record_path(record.scope) != Path(entry.path)
                ):
                    raise SubmissionRecordCorruptError(
                        "The state record path differs from its scope."
                    )
                records.append(record)
    require_submission_actor(user_id)
    return tuple(sorted(records, key=lambda record: (record.created_at, record.scope.operation_id)))


@contextmanager
def state_submission_lock(scope: StateScope) -> Iterator[None]:
    require_submission_actor(scope.user_id)
    path = _record_path(scope)
    _ensure_directory(path.parent)
    lock: BaseFileLock = FileLock(str(path.with_suffix(".lock")), timeout=0, mode=0o600)
    if type(lock) not in (UnixFileLock, WindowsFileLock):
        raise SubmissionRecordError("Native state submission locking is unavailable.")
    with lock:
        if type(lock) not in (UnixFileLock, WindowsFileLock):
            raise SubmissionRecordError("Native state submission locking is unavailable.")
        _directory_sync(path.parent)
        require_submission_actor(scope.user_id)
        yield


def _write(record: StateSubmission) -> None:
    path = _record_path(record.scope)
    stored = _Stored(
        record=record, digest=hashlib.sha256(record.model_dump_json().encode()).hexdigest()
    )
    encoded = stored.model_dump_json().encode()
    if len(encoded) > _MAX_BYTES:
        raise SubmissionRecordError("The state record exceeds its size limit.")
    fd, name = tempfile.mkstemp(prefix=".state-submission-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _directory_sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_state_submission(project_id: str, user_id: str, payload: bytes) -> StateSubmission:
    require_submission_actor(user_id)
    record = StateSubmission(
        scope=StateScope(project_id=project_id, user_id=user_id, operation_id=uuid.uuid4().hex),
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        payload_json=payload.decode("utf-8"),
        payload_digest=hashlib.sha256(payload).hexdigest(),
        phase="prepared",
    )
    with state_submission_lock(record.scope):
        if read_state_submission(record.scope) is not None:
            raise SubmissionRecordError("The generated state identity already exists.")
        _write(record)
        require_submission_actor(user_id)
    return record


def mark_state_uncertain(record: StateSubmission) -> StateSubmission:
    if record.phase == "resolved" or read_state_submission(record.scope) != record:
        raise SubmissionRecordError("The saved state submission cannot make this transition.")
    updated = StateSubmission.model_validate({**record.model_dump(), "phase": "uncertain"})
    _write(updated)
    return updated


def record_state_result(record: StateSubmission, result: StateResult) -> StateSubmission:
    if record.phase == "resolved" or read_state_submission(record.scope) != record:
        raise SubmissionRecordError("The saved state submission cannot make this transition.")
    updated = StateSubmission.model_validate(
        {
            **record.model_dump(),
            "phase": "uncertain" if result.unresolved else "resolved",
            "result": result,
        }
    )
    _write(updated)
    return updated
