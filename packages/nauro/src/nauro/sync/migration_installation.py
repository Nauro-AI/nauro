"""Continue one saved existing-hosted conversion through replica installation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from nauro.store.generation_installation import install_generation_root, publish_generation_control
from nauro.store.generation_projection import VerifiedGenerationProjection
from nauro.store.generation_refresh_io import RefreshPaths, durable_replace, sync_file, sync_parents
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    admission_path,
    current_source,
    migration_home,
    migration_lock,
    retained_source,
    same_store_binding,
)
from nauro.store.replica_control import _is_link_or_reparse, _validate_managed_path
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_attachment_record import validate_retained_projection
from nauro.sync.generation_refresh import (
    _authorize,
    admit_generation_store,
    commit_generation_refresh,
    prepare_generation_refresh,
    prepare_initial_generation_refresh,
)
from nauro.sync.migration_admission import load_migration_plan, verify_migration_source
from nauro.sync.migration_preservation import (
    _directories,
    _inspect_backup,
    _Plan,
    _require_registration,
    decode_migration_plan,
    preserve_migration_source,
)


def _vacant_store(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    return stat.S_ISDIR(info.st_mode) and not _is_link_or_reparse(info) and not any(path.iterdir())


def verify_admitted_migration_source(record: MigrationAdmission) -> None:
    """Refuse a replacing or installing upgrade whose retained source no longer matches."""
    if record.phase not in {"replacing", "installing"}:
        raise MigrationAdmissionError("Retained source checks require an admitted upgrade.")
    current, raw = load_migration_plan(Path(record.store))
    if current != record:
        raise MigrationAdmissionError("The saved upgrade changed; reopen connection setup.")
    source, location = Path(record.store), current_source(record)
    if (
        record.phase == "replacing"
        and record.source_id is None
        and source.exists()
        and not location.exists()
    ):
        location = source
    try:
        verify_migration_source(record.model_copy(update={"store": str(location)}), raw)
    except MigrationAdmissionError as exc:
        raise MigrationAdmissionError(
            "The retained project files changed after this upgrade was admitted."
        ) from exc


def _registration(record: MigrationAdmission, session: InitialAttachmentSession) -> None:
    _require_registration(record)
    if not same_store_binding(session.binding.store_path, Path(record.store)):
        raise MigrationAdmissionError("Conversion source binding differs.")
    if (
        session.binding.project_id != record.project_id
        or session.binding.server_url != record.endpoint
    ):
        raise MigrationAdmissionError("Conversion session binding differs.")
    session.require_binding(session.binding)
    session.require_actor(record.actor)


def _advance(record: MigrationAdmission, raw: bytes, phase: str) -> MigrationAdmission:
    source = Path(record.store)
    if load_migration_plan(source) != (record, raw):
        raise MigrationAdmissionError("Conversion evidence changed.")
    desired = MigrationAdmission.model_validate({**record.model_dump(), "phase": phase})
    home = migration_home()
    paths = RefreshPaths(home, home)
    durable_replace(paths, admission_path(source), desired.canonical_bytes())
    sync_file(paths, admission_path(source))
    sync_parents(paths, home)
    return desired


def _sync_admission(record: MigrationAdmission, raw: bytes) -> None:
    source, home = Path(record.store), migration_home()
    paths = RefreshPaths(home, home)
    sync_file(paths, admission_path(source))
    sync_parents(paths, home)
    if load_migration_plan(source) != (record, raw):
        raise MigrationAdmissionError("Conversion evidence changed during durability checks.")


def _backup(record: MigrationAdmission, plan: _Plan, raw: bytes) -> None:
    root = Path(record.store).parent / plan.backup_directory_name
    found = _inspect_backup(root, plan, raw)
    if found != {"plan.json", *(entry.destination_path for entry in plan.entries)}:
        raise MigrationAdmissionError("Conversion requires complete preservation.")
    paths = RefreshPaths(root.parent, root.parent)
    for name in sorted(found):
        sync_file(paths, root / name)
    for name in sorted(_directories(plan), reverse=True):
        sync_parents(paths, root / name)
    sync_parents(paths, root)


def _replace_source(record: MigrationAdmission, raw: bytes) -> None:
    source, retained = Path(record.store), retained_source(record)
    _validate_managed_path(source.parent, retained)
    if record.source_id is not None:
        if not retained.exists() or not _vacant_store(source):
            raise MigrationAdmissionError("Relocated source evidence differs; evidence retained.")
    elif source.exists():
        if retained.exists():
            raise MigrationAdmissionError("Both source locations exist; evidence retained.")
        verify_migration_source(record, raw)
        os.rename(source, retained)
    elif not retained.exists():
        raise MigrationAdmissionError("Both source locations are missing; evidence retained.")
    verify_migration_source(record.model_copy(update={"store": str(retained)}), raw)
    paths = RefreshPaths(source.parent, source.parent)
    plan = decode_migration_plan(raw, record)
    for entry in plan.entries:
        sync_file(paths, retained / entry.source_path)
    for directory in sorted(plan.directory_paths, reverse=True):
        sync_parents(paths, retained / directory)
    sync_parents(paths, retained)


def _install(projection: VerifiedGenerationProjection, session: InitialAttachmentSession) -> None:
    binding = projection.target.binding
    binding.store_path.mkdir(mode=0o700, exist_ok=True)
    has_refresh = validate_retained_projection(projection)
    if has_refresh:
        prepared = prepare_generation_refresh(binding, actor=session.actor, session=session)
    else:
        installed = install_generation_root(projection, timeout=0)
        publish_generation_control(installed, timeout=0, session=session)
        prepared = prepare_initial_generation_refresh(
            binding, actor=session.actor, session=session, acquired=projection
        )
    if prepared.projection.target != projection.target:
        raise MigrationAdmissionError("Conversion target changed; evidence retained.")
    commit_generation_refresh(prepared, session=session)
    admit_generation_store(binding, actor=session.actor, session=session)
    parent = binding.store_path.parent
    sync_parents(RefreshPaths(parent, parent), binding.store_path)


def continue_migration_installation(
    record: MigrationAdmission,
    projection: VerifiedGenerationProjection,
    session: InitialAttachmentSession,
) -> MigrationAdmission:
    """Explicit continuation; inspection alone never moves or installs a source."""
    if (
        type(record) is not MigrationAdmission
        or type(projection) is not VerifiedGenerationProjection
        or not isinstance(session, InitialAttachmentSession)
    ):
        raise MigrationAdmissionError(
            "Conversion requires saved evidence and a verified owner projection."
        )
    if record.phase not in {"blocked", "replacing", "installing", "completed"}:
        raise MigrationAdmissionError("Conversion has not been admitted.")
    if projection.target.binding != session.binding:
        raise MigrationAdmissionError("Conversion projection binding differs.")
    source = Path(record.store)
    if record.phase == "blocked":
        preserve_migration_source(record, session)
    with migration_lock(source):
        current, raw = load_migration_plan(source)
        if current != record:
            raise MigrationAdmissionError("Inspect the current conversion before continuing.")
        _sync_admission(record, raw)
        plan = decode_migration_plan(raw, record)
        _registration(record, session)
        if record.phase == "completed":
            _backup(record, plan, raw)
            verify_migration_source(
                record.model_copy(update={"store": str(retained_source(record))}), raw
            )
            admit_generation_store(session.binding, actor=record.actor, session=session)
            return record
        if plan.projection != projection.target.identity:
            raise MigrationAdmissionError("Conversion projection differs from the saved plan.")
        _registration(record, session)
        _authorize(projection.target, session)
        _backup(record, plan, raw)
        if record.phase == "blocked":
            located = record.model_copy(update={"store": str(current_source(record))})
            verify_migration_source(located, raw)
            record = _advance(record, raw, "replacing")
        if record.phase == "replacing":
            _replace_source(record, raw)
            record = _advance(record, raw, "installing")
        located = record.model_copy(update={"store": str(retained_source(record))})
        verify_migration_source(located, raw)
        if record.phase == "installing":
            _registration(record, session)
            _authorize(projection.target, session)
            _install(projection, session)
            _registration(record, session)
            _authorize(projection.target, session)
            record = _advance(record, raw, "completed")
        return record
