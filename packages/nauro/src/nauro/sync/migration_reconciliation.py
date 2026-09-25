"""Reassess an admitted upgrade; the earlier record, plan and backup stay as evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

from nauro.store.generation_migration_assessment import (
    LegacyFileStamp,
    assess_preserved_legacy_source,
)
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    migration_lock,
    retained_source,
)
from nauro.store.replica_control import ReplicaControlBusyError
from nauro.sync.generation_acquisition import acquire_generation_projection
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_refresh import _authorize
from nauro.sync.migration_admission import (
    _publish_successor,
    _require_replacement_binding,
    load_migration_plan,
)
from nauro.sync.migration_installation import _registration, _vacant_store
from nauro.sync.migration_preservation import _inspect_backup, decode_migration_plan

_RECOVERY = "owner recovery is required."


def _locate(record: MigrationAdmission) -> tuple[Path, bool]:
    store, retained = Path(record.store), retained_source(record)
    if record.source_id is None and record.phase in {"blocked", "reassessed"}:
        if retained.exists():
            raise MigrationAdmissionError("Both source locations exist; " + _RECOVERY)
        return store, False
    if record.phase == "replacing" and record.source_id is None:
        if store.exists() == retained.exists():
            raise MigrationAdmissionError("The source location is ambiguous; " + _RECOVERY)
        if store.exists():
            return store, False
    if not retained.exists():
        raise MigrationAdmissionError("The retained source is missing; " + _RECOVERY)
    if not _vacant_store(store):
        raise MigrationAdmissionError(
            "Replica installation evidence for the earlier record is present; " + _RECOVERY
        )
    return retained, True


def reconcile_admitted_migration(
    expected: MigrationAdmission, session: InitialAttachmentSession
) -> MigrationAdmission:
    """Replace an admitted upgrade with a reassessed one that needs fresh consent."""
    if (
        type(expected) is not MigrationAdmission
        or expected.phase not in {"blocked", "replacing", "installing", "reassessed"}
        or not isinstance(session, InitialAttachmentSession)
    ):
        raise MigrationAdmissionError("Reassessment requires an admitted upgrade.")
    session.require_binding(session.binding)
    _registration(expected, session)
    projection = acquire_generation_projection(
        session.binding, active_user_id=expected.actor, session=session
    )
    store = Path(expected.store)
    try:
        with migration_lock(store, timeout=0):
            current, old_raw = load_migration_plan(store)
            if current != expected:
                raise MigrationAdmissionError("The saved upgrade changed; reopen connection setup.")
            old = decode_migration_plan(old_raw, expected)
            location, relocated = _locate(expected)
            found = _inspect_backup(store.parent / old.backup_directory_name, old, old_raw)
            complete = {"plan.json", *(entry.destination_path for entry in old.entries)}
            if expected.phase not in {"blocked", "reassessed"} and found != complete:
                raise MigrationAdmissionError("Earlier preservation is incomplete; " + _RECOVERY)
            assessment = assess_preserved_legacy_source(projection, location)
            stamps = tuple(
                sorted(LegacyFileStamp(e.source_path, e.size, e.sha256) for e in old.entries)
            )
            files = (
                *assessment.protected_files,
                *assessment.snapshot_files,
                *assessment.evidence_files,
            )
            changed = tuple(sorted(files)) != stamps or assessment.directory_paths != tuple(
                old.directory_paths
            )
            if not changed and old.projection == projection.target.identity:
                raise MigrationAdmissionError("Nothing changed; continue the saved upgrade.")
            if changed and (relocated or expected.phase not in {"blocked", "reassessed"}):
                raise MigrationAdmissionError(
                    "The retained project files changed after this upgrade was admitted; "
                    + _RECOVERY
                )
            plan = prepare_legacy_migration_plan(assessment)
            _authorize(projection.target, session)
            session.require_actor(expected.actor)
            successor = MigrationAdmission(
                migration_id=plan.migration_id,
                project_id=expected.project_id,
                actor=expected.actor,
                endpoint=expected.endpoint,
                store=expected.store,
                plan_digest=plan.plan_digest,
                predecessor_digest=hashlib.sha256(expected.canonical_bytes()).hexdigest(),
                phase="reassessed",
                source_id=expected.source_id or (expected.migration_id if relocated else None),
            )
            _require_replacement_binding(expected, successor)
            decode_migration_plan(plan.manifest_json, successor)
            _publish_successor(store, expected, successor, plan.manifest_json)
            if load_migration_plan(store) != (successor, plan.manifest_json):
                raise MigrationAdmissionError("The reassessed upgrade was not retained.")
            return successor
    except ReplicaControlBusyError as exc:
        raise MigrationAdmissionError("Another local operation is busy; try again later.") from exc
