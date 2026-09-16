"""Immutable recovery intentions retained outside project synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.operations.commit_plan import canonical_judgment_payload_bytes
from nauro_core.provenance import validate_utc_timestamp
from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from nauro.store.home import nauro_home
from nauro.store.submission_records import (
    Digest,
    SubmissionScope,
    _ClosedModel,
    _directory_sync,
    _ensure_directory,
)


def timestamp(value: str) -> datetime:
    validate_utc_timestamp(value, field="recovery_timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RecoveryBinding(_ClosedModel):
    original_scope: SubmissionScope
    original_payload_digest: Digest
    expected_state: Literal["active", "recovery_required"]
    expected_fence: StrictInt = Field(ge=1)
    expected_lease_owner: StrictStr | None
    expected_lease_expires_at: StrictStr | None
    created_at: StrictStr
    admission_deadline: StrictStr

    @model_validator(mode="after")
    def _window(self) -> RecoveryBinding:
        if timestamp(self.admission_deadline) - timestamp(self.created_at) != timedelta(days=1):
            raise ValueError("Recovery requires the original 24-hour admission window")
        if self.expected_state == "active":
            if self.expected_lease_owner is None or self.expected_lease_expires_at is None:
                raise ValueError("Active recovery requires its exact lease")
            validate_identifier(IdentifierKind.ulid, self.expected_lease_owner, field="lease")
            timestamp(self.expected_lease_expires_at)
        elif self.expected_lease_owner is not None or self.expected_lease_expires_at is not None:
            raise ValueError("Recovery-required observations cannot carry a lease")
        return self


class RecoveryPayload(_ClosedModel):
    schema_name: Literal["nauro.judgment_recovery.v2"] = Field(alias="schema")
    saga_id: StrictStr
    disposition: Literal["resume", "abandon"]
    binding: RecoveryBinding

    @field_validator("saga_id")
    @classmethod
    def _saga(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="saga_id")

    def canonical(self) -> str:
        return canonical_judgment_payload_bytes(
            self.model_dump(mode="json", by_alias=True)
        ).decode()


class RecoveryAction(_ClosedModel):
    version: Literal[1] = 1
    connection_binding: Digest
    project_id: StrictStr
    actor_id: StrictStr
    action_id: StrictStr
    action_payload: StrictStr
    payload_digest: Digest
    execution_deadline: StrictStr | None

    @field_validator("project_id", "actor_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="recovery_identity")

    @field_validator("action_id")
    @classmethod
    def _action(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.operation_id, value, field="action_id")

    @property
    def payload(self) -> RecoveryPayload:
        return RecoveryPayload.model_validate_json(self.action_payload)

    @model_validator(mode="after")
    def _exact(self) -> RecoveryAction:
        payload = self.payload
        if (
            payload.canonical() != self.action_payload
            or hashlib.sha256(self.action_payload.encode()).hexdigest() != self.payload_digest
            or payload.binding.original_scope.project_id != self.project_id
        ):
            raise ValueError("Recovery action bytes or scope differ")
        if self.execution_deadline is not None:
            timestamp(self.execution_deadline)
        return self

    def require_window(self, now: datetime) -> None:
        binding = self.payload.binding
        deadline = timestamp(binding.admission_deadline)
        if self.payload.disposition == "resume" and self.execution_deadline is not None:
            deadline = min(deadline, timestamp(self.execution_deadline))
        if not timestamp(binding.created_at) <= now < deadline:
            raise ValueError("The saved recovery action is outside its admission window")


class RecoveryActionStore:
    def __init__(self, connection_binding: str, project: str, actor: str) -> None:
        for value in (project, actor):
            validate_identifier(IdentifierKind.ulid, value, field="recovery_identity")
        if len(connection_binding) != 64 or any(
            c not in "0123456789abcdef" for c in connection_binding
        ):
            raise ValueError("Invalid connection binding")
        self.binding, self.project, self.actor = connection_binding, project, actor
        self.directory = nauro_home() / "recovery-actions" / connection_binding / project / actor

    def _path(self, action_id: str) -> Path:
        validate_identifier(IdentifierKind.operation_id, action_id, field="action_id")
        return self.directory / (hashlib.sha256(action_id.encode()).hexdigest() + ".json")

    def _check(self, record: RecoveryAction) -> None:
        if (record.connection_binding, record.project_id, record.actor_id) != (
            self.binding,
            self.project,
            self.actor,
        ):
            raise ValueError("Saved action belongs to another connection, project or actor")

    def _directory(self) -> None:
        if os.name != "posix":
            raise ValueError("Recovery requires POSIX durable private storage")
        _ensure_directory(self.directory)
        for directory in (self.directory, *self.directory.parents):
            info = directory.lstat()
            if directory == nauro_home().parent:
                break
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Recovery records require owner-only directories")

    def read(self, action_id: str) -> RecoveryAction:
        self._directory()
        path = self._path(action_id)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Unsafe recovery action file")
            raw = stream.read(65537)
            os.fsync(stream.fileno())
        if len(raw) > 65536:
            raise ValueError("Recovery action exceeds size limit")
        record = RecoveryAction.model_validate_json(raw)
        if record.model_dump_json().encode() != raw or record.action_id != action_id:
            raise ValueError("Saved action encoding or identity differs")
        self._check(record)
        # Re-establish durability after a process stopped at the final barrier.
        _directory_sync(self.directory)
        return record

    def save(self, record: RecoveryAction) -> None:
        record = RecoveryAction.model_validate(record)
        self._check(record)
        self._directory()
        raw = record.model_dump_json().encode()
        if len(raw) > 65536:
            raise ValueError("Recovery action exceeds size limit")
        fd, name = tempfile.mkstemp(prefix=".action-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, self._path(record.action_id))
            _directory_sync(self.directory)
        finally:
            temporary.unlink(missing_ok=True)

    def list(self) -> tuple[RecoveryAction, ...]:
        self._directory()
        records = []
        for path in sorted(self.directory.glob("*.json")):
            if path.is_symlink():
                raise ValueError("Recovery action links are refused")
            # The bounded reader verifies the complete record before exposing it.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Recovery action is not a regular file")
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("Recovery action exceeds size limit")
            action_id = json.loads(raw)["action_id"]
            if self._path(action_id) != path:
                raise ValueError("Recovery action path differs")
            records.append(self.read(action_id))
        return tuple(records)
