"""Saved local conversion admission; inspection never continues execution."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import local
from typing import Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from nauro.store.home import nauro_home
from nauro.store.replica_control import (
    ReplicaControlBusyError,
    _is_link_or_reparse,
    _native_control_lock,
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
    phase: Literal[
        "assessed", "blocked", "declined", "reassessed", "replacing", "installing", "completed"
    ]
    source_id: str | None = None

    def canonical_bytes(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode()

    @field_validator("migration_id", "project_id", "actor")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="migration identity")

    @field_validator("source_id")
    @classmethod
    def source(cls, value: str | None) -> str | None:
        return None if value is None else cls.identifiers(value)

    @model_validator(mode="after")
    def relocated_source(self) -> MigrationAdmission:
        if self.source_id is not None and self.predecessor_digest is None:
            raise ValueError("A relocated source requires predecessor evidence")
        return self

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


def retained_source(record: MigrationAdmission) -> Path:
    source = Path(record.store)
    name = record.source_id or record.migration_id
    return source.parent / f"legacy-source-{record.project_id}-{name}"


def current_source(record: MigrationAdmission) -> Path:
    if record.source_id is None and record.phase in {
        "assessed",
        "declined",
        "blocked",
        "reassessed",
    }:
        return Path(record.store)
    return retained_source(record)


def migration_home() -> Path:
    return nauro_home().resolve()


MAX_ADMISSION_RECORDS = 128


def _named_path(store: Path) -> Path:
    key = hashlib.sha256(str(store.absolute()).encode()).hexdigest()
    return migration_home() / f"migration-{key}.json"


def admission_path(store: Path) -> Path:
    try:
        return _find_admission_path(store)
    except (OSError, ValueError, RuntimeError) as exc:
        raise MigrationAdmissionError(
            "Migration evidence is unavailable; inspect setup recovery."
        ) from exc


def same_store_binding(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    if left.name != right.name or not left.parent.samefile(right.parent):
        return False
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return not left.exists() and not right.exists()


def _find_admission_path(store: Path) -> Path:
    home = migration_home()
    matches: list[Path] = []
    try:
        home.stat()
    except FileNotFoundError:
        return _named_path(store.resolve())
    count = 0
    for path in home.iterdir():
        if not path.name.startswith("migration-") or path.suffix != ".json":
            continue
        suffix = path.stem.removeprefix("migration-")
        if len(suffix) != 64 or any(c not in "0123456789abcdef" for c in suffix):
            continue
        count += 1
        if count > MAX_ADMISSION_RECORDS:
            raise ValueError("Migration admission inspection limit exceeded")
        _validate_managed_path(home, path)
        raw = _read_optional_file(path)
        if raw is None:
            raise ValueError("Migration evidence disappeared")
        record = MigrationAdmission.model_validate_json(raw)
        recorded = Path(record.store)
        if (
            not recorded.is_absolute()
            or record.canonical_bytes() != raw
            or record.project_id != recorded.name
            or path != _named_path(recorded)
        ):
            raise ValueError("Migration admission evidence differs")
        if record.project_id == store.resolve().name.upper():
            if not same_store_binding(recorded, store):
                raise ValueError("Retained migration binding is uncertain")
            matches.append(path)
    if len(matches) > 1:
        raise ValueError("Multiple migration records identify this store")
    return matches[0] if matches else _named_path(store.resolve())


def _decode_record(store: Path, raw: bytes) -> MigrationAdmission:
    record = MigrationAdmission.model_validate_json(raw)
    if (
        record.canonical_bytes() != raw
        or not same_store_binding(Path(record.store), store)
        or record.project_id != store.name
    ):
        raise ValueError("Migration admission binding differs")
    return record


def inspect_migration(store: Path) -> MigrationAdmission | None:
    path = admission_path(store)
    try:
        _validate_managed_path(migration_home(), path)
        raw = _read_optional_file(path)
        record = None if raw is None else _decode_record(store, raw)
    except (OSError, ValueError) as exc:
        raise MigrationAdmissionError(
            "Migration evidence is unavailable; inspect setup recovery."
        ) from exc
    return record


def require_migration_admission(store: Path) -> None:
    record = inspect_migration(store)
    if record is not None and record.phase in {
        "blocked",
        "reassessed",
        "replacing",
        "installing",
    }:
        raise MigrationAdmissionError("Project conversion is incomplete; reopen connection setup.")

    if record is not None and record.phase == "completed":
        from nauro.store.generation_authority import _parse_marker

        path = store / ".replica/authority.json"
        _validate_managed_path(store, path)
        raw = _read_optional_file(path)
        if raw is None:
            raise MigrationAdmissionError("Completed conversion lost generation authority.")
        marker = _parse_marker(raw)
        if marker.project_id != record.project_id or marker.canonical_bytes() != raw:
            raise MigrationAdmissionError("Completed conversion authority differs.")


def migration_lock_path(store: Path) -> Path:
    try:
        project = validate_identifier(
            IdentifierKind.ulid, store.resolve().name.upper(), field="project"
        )
    except ValueError:
        project = "unidentified"
    path = migration_home() / f"migration-project-{project}.lock"
    for candidate in (path, _named_path(store.resolve()).with_suffix(".lock")):
        _validate_managed_path(migration_home(), candidate)
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            info.st_size
            or info.st_nlink != 1
            or _is_link_or_reparse(info)
            or not stat.S_ISREG(info.st_mode)
        ):
            raise MigrationAdmissionError("Migration lock contains unsafe evidence.")
    return path


_held_locks = local()


@contextmanager
def migration_lock(store: Path, *, timeout: float = 0) -> Iterator[None]:
    home = migration_home()
    _validate_managed_path(home, home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = migration_lock_path(store)
    held = getattr(_held_locks, "paths", None)
    if held is None:
        held = _held_locks.paths = set()
    if path in held:
        yield
        migration_lock_path(store)
        return
    with _native_control_lock(home, path, timeout):
        migration_lock_path(store)
        held.add(path)
        try:
            yield
            migration_lock_path(store)
        finally:
            held.remove(path)


@contextmanager
def migration_write_guard(store: Path, *, timeout: float = 10) -> Iterator[None]:
    try:
        with migration_lock(store, timeout=timeout):
            require_migration_admission(store)
            yield
    except ReplicaControlBusyError as exc:
        raise MigrationAdmissionError("Another local operation is busy; try again later.") from exc
