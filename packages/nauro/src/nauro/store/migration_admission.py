"""Saved local conversion admission; inspection never continues execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier
from pydantic import BaseModel, ConfigDict, field_validator

from nauro.store.home import nauro_home
from nauro.store.replica_control import _read_optional_file, _validate_managed_path


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
    phase: Literal["assessed", "blocked", "declined"]

    @field_validator("migration_id", "project_id", "actor")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="migration identity")

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
        record.model_dump_json().encode() != raw
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
