"""Saved local conversion admission; inspection never continues execution."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from filelock import FileLock, Timeout
from nauro_core.identifiers import IdentifierKind, validate_identifier
from pydantic import BaseModel, ConfigDict, field_validator

from nauro.store.home import nauro_home
from nauro.store.replica_control import (
    _is_link_or_reparse,
    _read_optional_file,
    _validate_managed_path,
)


class MigrationAdmissionError(PermissionError):
    pass


class MigrationAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    migration_id: str
    project_id: str
    actor: str
    endpoint: str
    store: str
    plan_digest: str
    predecessor_digest: str | None = None
    phase: Literal["assessed", "blocked", "declined"]

    def canonical_bytes(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode()

    @field_validator("migration_id", "project_id", "actor")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="migration identity")

    @field_validator("predecessor_digest")
    @classmethod
    def predecessor(cls, value: str | None) -> str | None:
        return None if value is None else cls.digest(value)

    @field_validator("plan_digest")
    @classmethod
    def digest(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Invalid migration digest")
        return value


def admission_path(store: Path) -> Path:
    key = hashlib.sha256(str(store.absolute()).encode()).hexdigest()
    return nauro_home() / f"migration-{key}.json"


def _decode_record(store: Path, raw: bytes) -> MigrationAdmission:
    record = MigrationAdmission.model_validate_json(raw)
    if (
        record.canonical_bytes() != raw
        or record.store != str(store.absolute())
        or record.project_id != store.name
    ):
        raise ValueError("Migration admission binding differs")
    return record


def inspect_migration(store: Path) -> MigrationAdmission | None:
    path = admission_path(store)
    try:
        _validate_managed_path(nauro_home(), path)
        raw = _read_optional_file(path)
        record = None if raw is None else _decode_record(store, raw)
    except (OSError, ValueError) as exc:
        raise MigrationAdmissionError(
            "Migration evidence is unavailable; inspect setup recovery."
        ) from exc
    return record


def require_migration_admission(store: Path) -> None:
    record = inspect_migration(store)
    if record is not None and record.phase == "blocked":
        raise MigrationAdmissionError("Project conversion is incomplete; reopen connection setup.")


def migration_lock_path(store: Path) -> Path:
    path = admission_path(store).with_suffix(".lock")
    _validate_managed_path(nauro_home(), path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if (
        info.st_size
        or info.st_nlink != 1
        or _is_link_or_reparse(info)
        or not stat.S_ISREG(info.st_mode)
    ):
        raise MigrationAdmissionError("Migration lock contains unsafe evidence.")
    return path


@contextmanager
def migration_write_guard(store: Path, *, timeout: float = 10) -> Iterator[None]:
    home = nauro_home()
    _validate_managed_path(home, home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(migration_lock_path(store), timeout=10, is_singleton=True)
    try:
        lock.acquire(timeout=timeout)
    except Timeout as exc:
        raise MigrationAdmissionError("Another local operation is busy; try again later.") from exc
    try:
        require_migration_admission(store)
        yield
    finally:
        lock.release()
