"""Explicit preservation of an admitted legacy source, without relocation."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

from nauro.store import _platform_durability as durability
from nauro.store.generation_migration_assessment import _inventory, _stamp_file
from nauro.store.generation_migration_plan import _Entry, _Plan, decode_migration_plan
from nauro.store.generation_projection import GenerationProjectionTarget
from nauro.store.generation_refresh_io import RefreshPaths, sync_file, sync_parents
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    current_source,
    migration_lock,
    same_store_binding,
)
from nauro.store.registry import get_project_entry_v2, get_store_path_v2
from nauro.store.replica_control import _is_link_or_reparse, _validate_managed_path
from nauro.store.resolution import resolve_project_binding
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_refresh import authorize_installed_target
from nauro.sync.migration_admission import (
    load_expected_migration_plan,
    load_migration_plan,
    verify_migration_source,
)


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


def _plan_pending(raw: bytes) -> str:
    return ".plan-" + hashlib.sha256(raw).hexdigest() + ".pending"


def _flags(base: int) -> int:
    """Binary mode keeps Windows from translating newlines or stopping reads at 0x1A."""
    names = ("O_BINARY", "O_NOFOLLOW", "O_NONBLOCK")
    return base | sum(getattr(os, name, 0) for name in names)


def _publish_plan(root: Path, raw: bytes) -> None:
    scratch = root / _plan_pending(raw)
    _validate_managed_path(root, scratch)
    fd = os.open(scratch, _flags(os.O_RDWR | os.O_CREAT), 0o600)
    with os.fdopen(fd, "r+b") as handle:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or _is_link_or_reparse(info)
            or info.st_nlink != 1
            or not os.path.samestat(info, scratch.stat())
            or not raw.startswith(handle.read(len(raw) + 1))
        ):
            raise MigrationAdmissionError("Preservation plan scratch differs.")
        handle.seek(0)
        handle.write(raw)
        handle.flush()
        os.fsync(fd)
        _validate_managed_path(root, scratch)
        if not os.path.samestat(os.fstat(fd), scratch.stat()):
            raise MigrationAdmissionError("Preservation plan scratch changed.")
    durability.durable_rename(scratch, root / "plan.json")


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
    initial = _plan_pending(raw)
    for stamp in files:
        if stamp.path == initial:
            if (
                stamp.size > len(raw)
                or (root / stamp.path).stat().st_nlink != 1
                or not raw.startswith((root / stamp.path).read_bytes())
            ):
                raise MigrationAdmissionError("Preservation plan scratch differs.")
        elif stamp.path in scratch:
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
        or (
            "plan.json" not in found
            and (directories or any(stamp.path != initial for stamp in files))
        )
        or ("plan.json" in found and any(stamp.path == initial for stamp in files))
    ):
        raise MigrationAdmissionError("Unexpected preservation evidence.")
    return found


def complete_backup_names(plan: _Plan) -> set[str]:
    return {"plan.json", *(entry.destination_path for entry in plan.entries)}


def sync_complete_backup(root: Path, plan: _Plan, raw: bytes, *, message: str) -> None:
    """Refuse an incomplete backup with message, else flush its files, directories and root."""
    found = _inspect_backup(root, plan, raw)
    if found != complete_backup_names(plan):
        raise MigrationAdmissionError(message)
    paths = RefreshPaths(root.parent, root.parent)
    for path in sorted(found):
        sync_file(paths, root / path)
    for directory in sorted(_directories(plan), reverse=True):
        sync_parents(paths, root / directory)
    sync_parents(paths, root)


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
    descriptor = os.open(scratch, _flags(os.O_WRONLY | os.O_CREAT), 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _is_link_or_reparse(opened) or opened.st_nlink != 1:
            raise MigrationAdmissionError("Preservation scratch is unsafe.")
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            try:
                source_fd = os.open(src, _flags(os.O_RDONLY))
            except durability.REFUSED as exc:
                raise MigrationAdmissionError(durability.refusal("copy", src, exc)) from exc
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
    durability.durable_rename(scratch, destination)
    sync_parents(RefreshPaths(root.parent, root.parent), destination.parent)


def _require_registration(record: MigrationAdmission) -> None:
    entry = get_project_entry_v2(record.project_id)
    if entry is None or entry.mode != "cloud" or entry.server_url != record.endpoint:
        raise MigrationAdmissionError("Conversion registration differs.")
    registered = entry.bound_store_path(record.project_id) or get_store_path_v2(record.project_id)
    source = Path(record.store)
    _validate_managed_path(source.parent, source)
    if not same_store_binding(registered, source):
        raise MigrationAdmissionError("Conversion source binding differs.")


def require_registered_migration_source(record: MigrationAdmission) -> None:
    if current_source(record) != Path(record.store):
        _require_registration(record)
        return
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
        raw = load_expected_migration_plan(
            source, record, changed="Preservation admission changed."
        )
        plan = decode_migration_plan(raw, record)
        binding = session.binding
        if (binding.project_id, binding.server_url) != (
            record.project_id,
            record.endpoint,
        ) or not same_store_binding(binding.store_path, source):
            raise MigrationAdmissionError("Preservation session binding differs.")
        target = GenerationProjectionTarget(binding, plan.projection)
        require_registered_migration_source(record)
        authorize_installed_target(target, session)
        located = record.model_copy(update={"store": str(current_source(record))})
        verify_migration_source(located, raw)
        root = source.parent / plan.backup_directory_name
        found = _inspect_backup(root, plan, raw)
        _mkdir(source.parent, root)
        if "plan.json" not in found:
            _publish_plan(root, raw)
        paths = RefreshPaths(root.parent, root.parent)
        sync_file(paths, root / "plan.json")
        sync_parents(paths, root)
        for directory in sorted(_directories(plan)):
            _mkdir(root, root / directory)
        for entry in plan.entries:
            session.require_actor(record.actor)
            if entry.destination_path not in found:
                _copy(Path(located.store), root, entry)
        sync_complete_backup(root, plan, raw, message="Preservation is incomplete.")
        verify_migration_source(located, raw)
        require_registered_migration_source(record)
        authorize_installed_target(target, session)
        if load_migration_plan(source) != (record, raw):
            raise MigrationAdmissionError("Preservation admission changed.")
        return root
