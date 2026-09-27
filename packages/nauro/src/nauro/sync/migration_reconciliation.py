"""Reassess an admitted upgrade; the earlier record, plan and backup stay as evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Literal

from nauro.store import _platform_durability as durability
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_installation import (
    _GENERATIONS_DIR,
    _STAGING_DIR,
    _STAGING_TOKEN_BYTES,
    _root_key,
)
from nauro.store.generation_migration_assessment import (
    LegacyFileStamp,
    _require_empty_control_lock,
    assess_preserved_legacy_source,
)
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.generation_projection import GenerationProjectionTarget
from nauro.store.generation_read import capture_generation_root
from nauro.store.generation_refresh_io import RefreshPaths, sync_parents
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    migration_lock,
    retained_source,
)
from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
    ReplicaControlBusyError,
    _is_link_or_reparse,
    _validate_managed_path,
)
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_acquisition import acquire_generation_projection
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_attachment_record import validate_retained_projection
from nauro.sync.generation_refresh import _authorize
from nauro.sync.migration_admission import (
    _publish_successor,
    _read_plan,
    _require_replacement_binding,
    inspect_previous_migration,
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


def stale_replica_folder(record: MigrationAdmission) -> Path | None:
    """Where interrupted installation evidence would move, or None when the store is vacant."""
    store = Path(record.store)
    if record.phase != "installing" or _vacant_store(store):
        return None
    return store.parent / f"legacy-install-{record.project_id}-{record.migration_id}"


def _real_directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not _is_link_or_reparse(info)


def _entries(path: Path) -> dict[str, os.stat_result]:
    with os.scandir(path) as listing:
        return {entry.name: entry.stat(follow_symlinks=False) for entry in listing}


def _only_staging(record: MigrationAdmission, raw: bytes) -> bool:
    store = Path(record.store)
    identity = decode_migration_plan(raw, record).projection
    binding = ResolvedProjectBinding(
        store, record.project_id, record.project_id, "cloud", record.endpoint
    )
    prefix = _root_key(GenerationProjectionTarget(binding, identity)) + "-"
    info = os.lstat(store)
    same_device = info.st_dev == os.stat(store.parent).st_dev
    listing = _entries(store) if _real_directory(info) and same_device else {}
    if listing.pop(_REPLICA_CONTROL_LOCK_NAME, None) is not None:
        lock = os.lstat(store / _REPLICA_CONTROL_LOCK_NAME)
        if not stat.S_ISREG(lock.st_mode) or lock.st_size != 0 or lock.st_nlink != 1:
            return False
    chain = (
        _REPLICA_CONTROL_ROOT_NAME,
        f"v{identity.store_format_version}",
        "actors",
        identity.installed_for_user_id,
    )
    current = store
    for name in chain:
        if set(listing) != {name} or not _real_directory(listing[name]):
            return False
        current = current / name
        listing = _entries(current)
    generations = listing.pop(_GENERATIONS_DIR, None)
    empty = generations is None or (
        _real_directory(generations) and not _entries(current / _GENERATIONS_DIR)
    )
    exact = empty and set(listing) == {_STAGING_DIR} and _real_directory(listing[_STAGING_DIR])
    staged = list(_entries(current / _STAGING_DIR).items()) if exact else []
    if len(staged) != 1:
        return False
    name, entry = staged[0]
    token = name[len(prefix) :]
    return (
        name.startswith(prefix)
        and len(token) == _STAGING_TOKEN_BYTES * 2
        and all(character in "0123456789abcdef" for character in token)
        and _real_directory(entry)
    )


def stale_replica_shape(
    record: MigrationAdmission, raw: bytes
) -> Literal["root", "staging"] | None:
    """Staging when only the earlier plan's lone staging entry is left, else root."""
    if stale_replica_folder(record) is None:
        return None
    try:
        return "staging" if _only_staging(record, raw) else "root"
    except (GenerationAuthorityError, OSError):
        return "root"


def _require_earlier_replica(
    expected: MigrationAdmission,
    raw: bytes,
    session: InitialAttachmentSession,
    folder: Path,
    shape: Literal["root", "staging"],
) -> None:
    store = Path(expected.store)
    _validate_managed_path(store.parent, folder)
    info = store.lstat()
    if (
        not retained_source(expected).is_dir()
        or os.path.lexists(folder)
        or not stat.S_ISDIR(info.st_mode)
        or _is_link_or_reparse(info)
        or info.st_dev != store.parent.stat().st_dev
    ):
        raise MigrationAdmissionError("Installation evidence cannot be moved aside; " + _RECOVERY)
    if stale_replica_shape(expected, raw) != shape:
        raise MigrationAdmissionError(
            "Installation evidence changed before the move; reopen connection setup."
        )
    identity = decode_migration_plan(raw, expected).projection
    try:
        _require_empty_control_lock(store)
        if shape == "root":
            validate_retained_projection(
                capture_generation_root(GenerationProjectionTarget(session.binding, identity))
            )
    except (GenerationAuthorityError, OSError) as exc:
        raise MigrationAdmissionError(
            "Unrecognized installation evidence is preserved intact; " + _RECOVERY
        ) from exc


def set_aside_stale_replica(
    expected: MigrationAdmission,
    session: InitialAttachmentSession,
    *,
    shape: Literal["root", "staging"],
) -> Path:
    """Move the confirmed shape aside: a verified earlier root or a lone staging entry.

    Refuses unless the shape recomputed under the lock is the confirmed one.
    """
    folder = stale_replica_folder(expected) if type(expected) is MigrationAdmission else None
    if (
        folder is None
        or shape not in ("root", "staging")
        or not isinstance(session, InitialAttachmentSession)
    ):
        raise MigrationAdmissionError("No interrupted installation evidence to move aside.")
    session.require_binding(session.binding)
    _registration(expected, session)
    projection = acquire_generation_projection(
        session.binding, active_user_id=expected.actor, session=session
    )
    store = Path(expected.store)
    try:
        with migration_lock(store, timeout=0):
            current, raw = load_migration_plan(store)
            if current != expected:
                raise MigrationAdmissionError("The saved upgrade changed; reopen connection setup.")
            if decode_migration_plan(raw, expected).projection == projection.target.identity:
                raise MigrationAdmissionError(
                    "The earlier installation still matches the hosted record; continue it instead."
                )
            _require_earlier_replica(expected, raw, session, folder, shape)
            durability.durable_rename(store, folder, replace=False)
            sync_parents(RefreshPaths(store.parent, store.parent), store.parent)
            if load_migration_plan(store) != (expected, raw) or os.path.lexists(store):
                raise MigrationAdmissionError("The saved upgrade changed during the move.")
            return folder
    except ReplicaControlBusyError as exc:
        raise MigrationAdmissionError("Another local operation is busy; try again later.") from exc


def describe_reassessment(
    record: MigrationAdmission, raw: bytes
) -> tuple[str, str, list[str], list[str]]:
    """Earlier upgrade id, its preservation folder, reclassified paths and changed paths."""
    previous = inspect_previous_migration(record)
    if record.phase != "reassessed" or previous is None:
        raise MigrationAdmissionError("Only a reassessed upgrade replaces an earlier one.")
    old = decode_migration_plan(_read_plan(previous), previous)
    before = {entry.source_path: entry for entry in old.entries}
    after = {entry.source_path: entry for entry in decode_migration_plan(raw, record).entries}
    reclassified = [
        path
        for path in sorted(before.keys() & after.keys())
        if (before[path].source_class, before[path].disposition)
        != (after[path].source_class, after[path].disposition)
    ]
    changed = [
        path
        for path in sorted(before.keys() | after.keys())
        if path not in before
        or path not in after
        or (before[path].size, before[path].sha256) != (after[path].size, after[path].sha256)
    ]
    return previous.migration_id, old.backup_directory_name, reclassified, changed
