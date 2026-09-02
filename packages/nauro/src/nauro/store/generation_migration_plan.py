"""Immutable preservation plans for legacy hosted-store migration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier

from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_migration_assessment import (
    LegacyFileStamp,
    LegacyMigrationAssessment,
)
from nauro.store.repo_config import generate_ulid

LegacySourceClass = Literal["identical", "divergent", "local_only", "snapshot"]
LegacyDisposition = Literal["legacy_backup", "quarantine"]
LegacyRecoveryKind = Literal[
    "archive_only",
    "restore_only",
    "typed_resubmission",
    "preserved_unsupported",
]
LegacyRecoveryRoute = Literal[
    "flag_question",
    "project_frame",
    "propose_decision",
    "share_context",
    "update_stack",
    "update_state",
]

_PLAN_SCHEMA_VERSION = 1
_PLAN_TOKEN = object()
_TYPED_TOP_LEVEL_ROUTES: dict[str, tuple[LegacyRecoveryRoute, ...]] = {
    "open-questions.md": ("flag_question", "propose_decision"),
    "project.md": ("project_frame",),
    "stack.md": ("update_stack",),
    "state.md": ("update_state",),
    "state_current.md": ("update_state",),
}


class LegacyMigrationPlanError(GenerationAuthorityError):
    """A legacy preservation plan could not be derived without ambiguity."""

    code = "legacy_migration_plan_failed"


@dataclass(frozen=True, order=True)
class LegacyMigrationPlanEntry:
    """One exact legacy file move and its post-migration recovery classification."""

    source_path: str
    destination_path: str
    size: int
    sha256: str
    source_class: LegacySourceClass
    disposition: LegacyDisposition
    recovery_kind: LegacyRecoveryKind
    recovery_routes: tuple[LegacyRecoveryRoute, ...]
    offer_export: bool


@dataclass(frozen=True, eq=False)
class LegacyMigrationPlan:
    """A no-I/O preservation plan bound to one exact legacy assessment."""

    assessment: LegacyMigrationAssessment = field(repr=False)
    migration_id: str
    created_at: str
    backup_directory_name: str
    entries: tuple[LegacyMigrationPlanEntry, ...]
    manifest_json: bytes = field(repr=False)
    plan_digest: str
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if (
            _construction_token is not _PLAN_TOKEN
            or type(self.assessment) is not LegacyMigrationAssessment
            or type(self.entries) is not tuple
            or any(type(entry) is not LegacyMigrationPlanEntry for entry in self.entries)
            or type(self.manifest_json) is not bytes
            or hashlib.sha256(self.manifest_json).hexdigest() != self.plan_digest
        ):
            raise LegacyMigrationPlanError(
                "Legacy migration plans must come from a verified assessment."
            )

    @property
    def backup_root(self) -> Path:
        return (
            self.assessment.projection.target.binding.store_path.parent / self.backup_directory_name
        )

    @property
    def quarantine_entries(self) -> tuple[LegacyMigrationPlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.disposition == "quarantine")

    @property
    def backup_entries(self) -> tuple[LegacyMigrationPlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.disposition == "legacy_backup")


def _validate_assessment(assessment: LegacyMigrationAssessment) -> None:
    if type(assessment) is not LegacyMigrationAssessment:
        raise LegacyMigrationPlanError("Legacy migration planning requires an assessment.")
    if not assessment.ready_for_backup:
        raise LegacyMigrationPlanError(
            "Legacy migration planning requires settled pending and unsupported paths."
        )
    if type(assessment.protected_files) is not tuple or any(
        type(stamp) is not LegacyFileStamp for stamp in assessment.protected_files
    ):
        raise LegacyMigrationPlanError("The assessed protected inventory is malformed.")
    protected = {stamp.path: stamp for stamp in assessment.protected_files}
    if len(protected) != len(assessment.protected_files):
        raise LegacyMigrationPlanError("The assessed protected inventory is malformed.")
    classes = (
        set(assessment.identical_paths),
        set(assessment.divergent_paths),
        set(assessment.local_only_paths),
    )
    if any(classes[left] & classes[right] for left in range(3) for right in range(left + 1, 3)):
        raise LegacyMigrationPlanError("The assessed protected classifications overlap.")
    if set().union(*classes) != set(protected):
        raise LegacyMigrationPlanError("The assessed protected classifications are incomplete.")

    server = {artifact.path: artifact.digest for artifact in assessment.projection.artifacts}
    identical = {
        path for path in set(protected) & set(server) if protected[path].sha256 == server[path]
    }
    divergent = set(protected) & set(server) - identical
    local_only = set(protected) - set(server)
    server_only = set(server) - set(protected)
    expected = (identical, divergent, local_only, server_only)
    observed = (
        set(assessment.identical_paths),
        set(assessment.divergent_paths),
        set(assessment.local_only_paths),
        set(assessment.server_only_paths),
    )
    if observed != expected:
        raise LegacyMigrationPlanError("The assessed protected classifications diverge.")
    if type(assessment.snapshot_files) is not tuple or any(
        type(stamp) is not LegacyFileStamp or not stamp.path.startswith("snapshots/")
        for stamp in assessment.snapshot_files
    ):
        raise LegacyMigrationPlanError("The assessed snapshot inventory is malformed.")
    if len({stamp.path for stamp in assessment.snapshot_files}) != len(assessment.snapshot_files):
        raise LegacyMigrationPlanError("The assessed snapshot inventory is malformed.")


def _migration_identity() -> tuple[str, str, str]:
    observed = datetime.now(timezone.utc)
    created_at = observed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    filename_time = observed.strftime("%Y%m%dT%H%M%S%fZ")
    try:
        migration_id = validate_identifier(
            IdentifierKind.ulid,
            generate_ulid(),
            field="migration_id",
        )
    except ValueError as exc:
        raise LegacyMigrationPlanError("The migration identity could not be derived.") from exc
    return migration_id, created_at, filename_time


def _recovery_routes(path: str) -> tuple[LegacyRecoveryRoute, ...]:
    top_level = _TYPED_TOP_LEVEL_ROUTES.get(path)
    if top_level is not None:
        return top_level
    if path.startswith("context/"):
        return ("share_context",)
    if path.startswith("decisions/"):
        return ("propose_decision",)
    return ()


def _protected_entry(
    stamp: LegacyFileStamp,
    source_class: Literal["identical", "divergent", "local_only"],
) -> LegacyMigrationPlanEntry:
    if source_class == "identical":
        return LegacyMigrationPlanEntry(
            stamp.path,
            f"legacy/{stamp.path}",
            stamp.size,
            stamp.sha256,
            source_class,
            "legacy_backup",
            "restore_only",
            (),
            False,
        )
    routes = _recovery_routes(stamp.path)
    return LegacyMigrationPlanEntry(
        stamp.path,
        f"quarantine/{stamp.path}",
        stamp.size,
        stamp.sha256,
        source_class,
        "quarantine",
        "typed_resubmission" if routes else "preserved_unsupported",
        routes,
        not routes,
    )


def _snapshot_entry(stamp: LegacyFileStamp) -> LegacyMigrationPlanEntry:
    return LegacyMigrationPlanEntry(
        stamp.path,
        f"legacy/{stamp.path}",
        stamp.size,
        stamp.sha256,
        "snapshot",
        "legacy_backup",
        "archive_only",
        (),
        True,
    )


def _entry_payload(entry: LegacyMigrationPlanEntry) -> dict[str, object]:
    return {
        "source_path": entry.source_path,
        "destination_path": entry.destination_path,
        "size": entry.size,
        "sha256": entry.sha256,
        "source_class": entry.source_class,
        "disposition": entry.disposition,
        "recovery_kind": entry.recovery_kind,
        "recovery_routes": entry.recovery_routes,
        "offer_export": entry.offer_export,
    }


def prepare_legacy_migration_plan(
    assessment: LegacyMigrationAssessment,
) -> LegacyMigrationPlan:
    """Bind every assessed legacy byte to backup or timestamped quarantine."""
    _validate_assessment(assessment)
    migration_id, created_at, filename_time = _migration_identity()
    project_id = assessment.projection.project_id
    backup_name = f"legacy-backup-{project_id}-{filename_time}-{migration_id}"
    classes: dict[str, Literal["identical", "divergent", "local_only"]] = {
        **{path: "identical" for path in assessment.identical_paths},
        **{path: "divergent" for path in assessment.divergent_paths},
        **{path: "local_only" for path in assessment.local_only_paths},
    }
    entries = tuple(
        sorted(
            [_protected_entry(stamp, classes[stamp.path]) for stamp in assessment.protected_files]
            + [_snapshot_entry(stamp) for stamp in assessment.snapshot_files]
        )
    )
    payload = {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "migration_id": migration_id,
        "created_at": created_at,
        "backup_directory_name": backup_name,
        "project_id": project_id,
        "assessment_capture_digest": assessment.capture_digest,
        "projection": assessment.projection.target.identity.model_dump(),
        "server_only_paths": assessment.server_only_paths,
        "entries": [_entry_payload(entry) for entry in entries],
    }
    manifest_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return LegacyMigrationPlan(
        assessment=assessment,
        migration_id=migration_id,
        created_at=created_at,
        backup_directory_name=backup_name,
        entries=entries,
        manifest_json=manifest_json,
        plan_digest=hashlib.sha256(manifest_json).hexdigest(),
        _construction_token=_PLAN_TOKEN,
    )


__all__ = [
    "LegacyMigrationPlan",
    "LegacyMigrationPlanEntry",
    "LegacyMigrationPlanError",
    "prepare_legacy_migration_plan",
]
