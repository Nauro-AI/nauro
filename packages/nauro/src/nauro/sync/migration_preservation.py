"""Explicit preservation of an admitted legacy source, without relocation."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nauro.store._atomic import atomic_write_bytes
from nauro.store.generation_migration_assessment import _inventory, _stamp_file
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
)
from nauro.store.generation_refresh_io import RefreshPaths, sync_file, sync_parents
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    migration_lock,
    same_store_binding,
)
from nauro.store.replica_control import _is_link_or_reparse, _validate_managed_path
from nauro.store.resolution import resolve_project_binding
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_refresh import _authorize
from nauro.sync.migration_admission import _verify_source, load_migration_plan


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_path: str
    destination_path: str
    size: int
    sha256: str
    source_class: Literal["identical", "divergent", "local_only", "snapshot", "evidence"]
    disposition: Literal["legacy_backup", "quarantine"]
    recovery_kind: Literal[
        "archive_only", "restore_only", "typed_resubmission", "preserved_unsupported"
    ]
    recovery_routes: list[Literal["propose_decision"]]
    offer_export: bool


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    migration_id: str
    created_at: str
    backup_directory_name: str
    project_id: str
    server_url: str
    directory_paths: list[str]
    assessment_capture_digest: str
    projection: GenerationProjectionIdentity
    server_only_paths: list[str]
    entries: list[_Entry]


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
    ):
        raise MigrationAdmissionError("Preservation path is unsafe.")
    return value


def _decode(raw: bytes, record: MigrationAdmission) -> _Plan:
    plan = _Plan.model_validate_json(raw)
    if (
        plan.migration_id != record.migration_id
        or plan.project_id != record.project_id
        or plan.server_url != record.endpoint
        or plan.projection.installed_for_user_id != record.actor
        or plan.projection.project_id != record.project_id
        or len({entry.source_path for entry in plan.entries}) != len(plan.entries)
    ):
        raise MigrationAdmissionError("Preservation plan binding differs.")
    name = _relative(plan.backup_directory_name)
    if (
        "/" in name
        or not name.startswith(f"legacy-backup-{record.project_id}-")
        or not name.endswith("-" + record.migration_id)
    ):
        raise MigrationAdmissionError("Preservation location differs.")
    for directory in plan.directory_paths:
        _relative(directory)
    for entry in plan.entries:
        prefix = "legacy" if entry.disposition == "legacy_backup" else "quarantine"
        if entry.destination_path != prefix + "/" + _relative(entry.source_path) or entry.size < 0:
            raise MigrationAdmissionError("Preservation entry differs.")
        MigrationAdmission.digest(entry.sha256)
    return plan


def _directories(plan: _Plan) -> set[str]:
    directories = {"legacy", "quarantine", ".pending"}
    for relative in [
        *("legacy/" + path for path in plan.directory_paths),
        *(entry.destination_path for entry in plan.entries),
    ]:
        path = PurePosixPath(relative)
        if relative.startswith("legacy/") and relative[7:] in plan.directory_paths:
            directories.add(relative)
        directories.update(
            parent.as_posix() for parent in path.parents if parent != PurePosixPath(".")
        )
    return directories


def _pending(entry: _Entry) -> str:
    return ".pending/" + hashlib.sha256(entry.source_path.encode()).hexdigest()


def _inspect_backup(root: Path, plan: _Plan, raw: bytes) -> set[str]:
    _validate_managed_path(root.parent, root)
    if not root.exists():
        return set()
    _validate_managed_path(root, root / ".replica-control.lock")
    if (root / ".replica-control.lock").exists():
        raise MigrationAdmissionError("Unexpected preservation control file.")
    files, directories, pending = _inventory(root)
    expected = {entry.destination_path: (entry.size, entry.sha256) for entry in plan.entries}
    expected["plan.json"] = (len(raw), hashlib.sha256(raw).hexdigest())
    scratch = {_pending(entry) for entry in plan.entries}
    found = set()
    for stamp in files:
        if stamp.path in scratch:
            if (root / stamp.path).stat().st_nlink != 1:
                raise MigrationAdmissionError("Preservation scratch has an alias.")
        elif (
            expected.get(stamp.path) != (stamp.size, stamp.sha256)
            or (root / stamp.path).stat().st_nlink != 1
        ):
            raise MigrationAdmissionError("Preserved evidence differs; retained unchanged.")
        else:
            found.add(stamp.path)
    if (
        pending
        or set(directories) - _directories(plan)
        or ((files or directories) and "plan.json" not in found)
    ):
        raise MigrationAdmissionError("Unexpected preservation evidence.")
    return found


def _mkdir(root: Path, path: Path) -> None:
    _validate_managed_path(root, path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _validate_managed_path(root, path)


def _copy(source: Path, root: Path, entry: _Entry) -> None:
    src, scratch, destination = (
        source / entry.source_path,
        root / _pending(entry),
        root / entry.destination_path,
    )
    _validate_managed_path(source, src)
    _validate_managed_path(root, scratch)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(scratch, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _is_link_or_reparse(opened) or opened.st_nlink != 1:
            raise MigrationAdmissionError("Preservation scratch is unsafe.")
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            source_fd = os.open(
                src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            )
            with os.fdopen(source_fd, "rb") as incoming:
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise MigrationAdmissionError("Preservation source is unsafe.")
                shutil.copyfileobj(incoming, output, length=1024 * 1024)
            output.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    stamp = _stamp_file(root, scratch)
    if (stamp.size, stamp.sha256) != (entry.size, entry.sha256):
        raise MigrationAdmissionError("Preservation source changed during copy.")
    _validate_managed_path(root, destination)
    if destination.exists():
        raise MigrationAdmissionError("Preservation destination became occupied.")
    os.replace(scratch, destination)
    sync_parents(RefreshPaths(root.parent, root.parent), destination.parent)


def _require_registered_source(record: MigrationAdmission) -> None:
    current = resolve_project_binding(record.project_id, None, use_cwd=False)
    if (
        current.mode != "cloud"
        or (
            current.project_id,
            current.server_url,
        )
        != (record.project_id, record.endpoint)
        or not same_store_binding(current.store_path, Path(record.store))
    ):
        raise MigrationAdmissionError("Registered preservation source differs.")


def preserve_migration_source(
    record: MigrationAdmission, session: InitialAttachmentSession
) -> Path:
    if (
        type(record) is not MigrationAdmission
        or record.phase != "blocked"
        or not isinstance(session, InitialAttachmentSession)
    ):
        raise MigrationAdmissionError("Preservation requires an admitted plan and owner session.")
    source = Path(record.store)
    with migration_lock(source):
        current, raw = load_migration_plan(source)
        if current != record:
            raise MigrationAdmissionError("Preservation admission changed.")
        plan = _decode(raw, record)
        binding = session.binding
        if (binding.project_id, binding.server_url) != (
            record.project_id,
            record.endpoint,
        ) or not same_store_binding(binding.store_path, source):
            raise MigrationAdmissionError("Preservation session binding differs.")
        target = GenerationProjectionTarget(binding, plan.projection)
        _require_registered_source(record)
        _authorize(target, session)
        _verify_source(record, raw)
        root = source.parent / plan.backup_directory_name
        found = _inspect_backup(root, plan, raw)
        _mkdir(source.parent, root)
        if "plan.json" not in found:
            atomic_write_bytes(root / "plan.json", raw)
        paths = RefreshPaths(root.parent, root.parent)
        sync_file(paths, root / "plan.json")
        sync_parents(paths, root)
        for directory in sorted(_directories(plan)):
            _mkdir(root, root / directory)
        for entry in plan.entries:
            session.require_actor(record.actor)
            if entry.destination_path not in found:
                _copy(source, root, entry)
        found = _inspect_backup(root, plan, raw)
        if found != {"plan.json", *(entry.destination_path for entry in plan.entries)}:
            raise MigrationAdmissionError("Preservation is incomplete.")
        for path in sorted(found):
            sync_file(paths, root / path)
        for directory in sorted(_directories(plan), reverse=True):
            sync_parents(paths, root / directory)
        sync_parents(paths, root)
        _verify_source(record, raw)
        _require_registered_source(record)
        _authorize(target, session)
        if load_migration_plan(source) != (record, raw):
            raise MigrationAdmissionError("Preservation admission changed.")
        return root
