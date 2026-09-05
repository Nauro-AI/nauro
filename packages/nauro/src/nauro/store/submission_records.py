"""Durable, actor-bound judgment submissions outside the project sync root."""

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
from nauro_core.operations.commit_plan import (
    HostedPreTeamApprovalPayloadV1,
    canonical_judgment_payload_bytes,
)
from nauro_core.provenance import validate_utc_timestamp
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from nauro.auth import read_active_user_id
from nauro.store.home import nauro_home

Digest = Annotated[StrictStr, Field(min_length=64, max_length=64, pattern="[0-9a-f]{64}")]
_MAX_RECORD_BYTES = 2 * 1024 * 1024


class SubmissionRecordError(RuntimeError):
    """A local submission cannot be read or durably stored."""


class SubmissionRecordCorruptError(SubmissionRecordError):
    """A stored submission fails its format or identity checks."""


class SubmissionActorMismatchError(SubmissionRecordError):
    """The active account does not own the saved operation."""


def require_submission_actor(user_id: str) -> None:
    if read_active_user_id() != user_id:
        raise SubmissionActorMismatchError("The active account does not own this submission.")


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class SubmissionScope(_ClosedModel):
    project_id: StrictStr
    user_id: StrictStr
    operation_kind: Literal["judgment_commit"] = "judgment_commit"
    operation_id: StrictStr

    @field_validator("project_id", "user_id")
    @classmethod
    def _ulid(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="submission_scope")

    @field_validator("operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.operation_id, value, field="operation_id")


class JudgmentTransportResult(_ClosedModel):
    """Adapter-verified evidence, not an HTTP response or public wire schema."""

    scope: SubmissionScope
    payload_digest: Digest
    status: Literal["absent", "pending", "committed", "stale", "expired", "disposed", "conflict"]
    receipt_json: StrictStr | None = None

    @model_validator(mode="after")
    def _receipt(self) -> JudgmentTransportResult:
        if self.status == "committed":
            if not self.receipt_json:
                raise ValueError("a committed result requires verified receipt bytes")
        elif self.receipt_json is not None:
            raise ValueError("only a committed result carries a receipt")
        return self


class JudgmentSubmission(_ClosedModel):
    schema_version: StrictInt = Field(ge=1, le=1, default=1)
    scope: SubmissionScope
    created_at: StrictStr
    approved_payload: StrictStr
    payload_digest: Digest
    phase: Literal["prepared", "uncertain", "resolved"]
    result: JudgmentTransportResult | None = None

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="created_at")

    @model_validator(mode="after")
    def _bindings(self) -> JudgmentSubmission:
        payload = HostedPreTeamApprovalPayloadV1.model_validate_json(
            self.payload_bytes, strict=True
        )
        if canonical_judgment_payload_bytes(payload.model_dump(mode="json")) != self.payload_bytes:
            raise ValueError("the approved payload must retain its canonical bytes")
        if hashlib.sha256(self.payload_bytes).hexdigest() != self.payload_digest:
            raise ValueError("the payload digest does not bind the approved bytes")
        if self.phase == "resolved":
            if self.result is None or self.result.status in {"absent", "pending"}:
                raise ValueError("a resolved submission requires a terminal result")
            if self.result.scope != self.scope or self.result.payload_digest != self.payload_digest:
                raise ValueError("the result does not bind the submission")
        elif self.result is not None:
            raise ValueError("an unresolved submission cannot carry a terminal result")
        return self

    @property
    def payload_bytes(self) -> bytes:
        return self.approved_payload.encode("utf-8")


class _StoredSubmission(_ClosedModel):
    record: JudgmentSubmission
    digest: Digest

    @model_validator(mode="after")
    def _integrity(self) -> _StoredSubmission:
        encoded = self.record.model_dump_json().encode()
        if hashlib.sha256(encoded).hexdigest() != self.digest:
            raise ValueError("the stored submission digest does not bind its record")
        return self


def _directory_sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(path: Path) -> None:
    if path.parent != path:
        _ensure_directory(path.parent)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise SubmissionRecordError("The submission directory is not a directory.")
    else:
        if not stat.S_ISDIR(mode):
            raise SubmissionRecordError("The submission directory is not a directory.")
    _directory_sync(path.parent)


def _scope_directory(project_id: str, user_id: str) -> Path:
    validate_identifier(IdentifierKind.ulid, project_id, field="project_id")
    validate_identifier(IdentifierKind.ulid, user_id, field="user_id")
    return nauro_home() / "submission-records" / project_id / user_id


def _record_path(scope: SubmissionScope) -> Path:
    key = hashlib.sha256(scope.model_dump_json().encode()).hexdigest()
    return _scope_directory(scope.project_id, scope.user_id) / f"{key}.json"


def _read(path: Path) -> JudgmentSubmission | None:
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SubmissionRecordCorruptError("The submission record is not a regular file.")
            encoded = handle.read(_MAX_RECORD_BYTES + 1)
        if len(encoded) > _MAX_RECORD_BYTES:
            raise SubmissionRecordCorruptError("The submission record exceeds its size bound.")
        stored = _StoredSubmission.model_validate_json(encoded, strict=True)
        if stored.model_dump_json().encode() != encoded:
            raise SubmissionRecordCorruptError("The submission record encoding is invalid.")
        return stored.record
    except (ValidationError, UnicodeError) as exc:
        raise SubmissionRecordCorruptError("The submission record is malformed.") from exc


def read_submission(scope: SubmissionScope) -> JudgmentSubmission | None:
    """Distinguish a missing record from unreadable or corrupt evidence."""
    require_submission_actor(scope.user_id)
    record = _read(_record_path(scope))
    if record is not None and record.scope != scope:
        raise SubmissionRecordCorruptError("The submission belongs to another scope.")
    return record


def list_submissions(project_id: str, user_id: str) -> tuple[JudgmentSubmission, ...]:
    """Discover recoverable identities after a process exits before returning an ID."""
    require_submission_actor(user_id)
    directory = _scope_directory(project_id, user_id)
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return ()
    records = []
    with entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                record = _read(Path(entry.path))
                if record is None or _record_path(record.scope) != Path(entry.path):
                    raise SubmissionRecordCorruptError(
                        "The submission path does not bind its scope."
                    )
                records.append(record)
    return tuple(sorted(records, key=lambda record: (record.created_at, record.scope.operation_id)))


@contextmanager
def submission_lock(scope: SubmissionScope) -> Iterator[None]:
    """Serialize one item's durable transitions and transport calls."""
    require_submission_actor(scope.user_id)
    path = _record_path(scope)
    _ensure_directory(path.parent)
    lock: BaseFileLock = FileLock(str(path.with_suffix(".lock")), timeout=0, mode=0o600)
    if type(lock) not in (UnixFileLock, WindowsFileLock):
        raise SubmissionRecordError("Native submission locking is unavailable.")
    with lock:
        if type(lock) not in (UnixFileLock, WindowsFileLock):
            raise SubmissionRecordError("Native submission locking is unavailable.")
        _directory_sync(path.parent)
        yield


def _write(record: JudgmentSubmission) -> None:
    path = _record_path(record.scope)
    stored = _StoredSubmission(
        record=record, digest=hashlib.sha256(record.model_dump_json().encode()).hexdigest()
    )
    encoded = stored.model_dump_json().encode()
    if len(encoded) > _MAX_RECORD_BYTES:
        raise SubmissionRecordError("The submission record exceeds its size bound.")
    fd, name = tempfile.mkstemp(prefix=".submission-", dir=path.parent)
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


def prepare_submission(
    project_id: str, user_id: str, approved_payload: bytes
) -> JudgmentSubmission:
    """Persist one explicitly approved operation before any network access."""
    require_submission_actor(user_id)
    record = JudgmentSubmission(
        scope=SubmissionScope(
            project_id=project_id, user_id=user_id, operation_id=uuid.uuid4().hex
        ),
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        approved_payload=approved_payload.decode("utf-8"),
        payload_digest=hashlib.sha256(approved_payload).hexdigest(),
        phase="prepared",
    )
    with submission_lock(record.scope):
        if read_submission(record.scope) is not None:
            raise SubmissionRecordError("The generated submission identity already exists.")
        _write(record)
    return record


def mark_uncertain(record: JudgmentSubmission) -> JudgmentSubmission:
    """Persist send intent while the caller holds the submission lock."""
    if read_submission(record.scope) != record:
        raise SubmissionRecordError("The saved submission changed before its transition.")
    updated = JudgmentSubmission.model_validate({**record.model_dump(), "phase": "uncertain"})
    _write(updated)
    return updated


def record_result(
    record: JudgmentSubmission, result: JudgmentTransportResult
) -> JudgmentSubmission:
    """Persist terminal evidence while the caller holds the submission lock."""
    if read_submission(record.scope) != record:
        raise SubmissionRecordError("The saved submission changed before its transition.")
    updated = JudgmentSubmission.model_validate(
        {**record.model_dump(), "phase": "resolved", "result": result}
    )
    _write(updated)
    return updated
