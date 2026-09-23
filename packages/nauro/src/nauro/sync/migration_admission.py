"""Persist exact preservation evidence before blocking local admission."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from filelock import FileLock

from nauro.store._atomic import atomic_write_bytes
from nauro.store.generation_installation import _read_expected
from nauro.store.generation_migration_plan import LegacyMigrationPlan
from nauro.store.generation_refresh_io import RefreshPaths, durable_replace, sync_file, sync_parents
from nauro.store.home import nauro_home
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    admission_path,
    inspect_migration,
)
from nauro.store.replica_control import _is_link_or_reparse, _validate_managed_path

MAX_PLAN_BYTES = 16 * 1024 * 1024


def _plan_path(record: MigrationAdmission) -> Path:
    return nauro_home() / f"migration-plan-{record.migration_id}.json"


def _read_plan(record: MigrationAdmission) -> bytes:
    path = _plan_path(record)
    _validate_managed_path(nauro_home(), path)
    info = path.lstat()
    if (
        _is_link_or_reparse(info)
        or info.st_nlink != 1
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > MAX_PLAN_BYTES
    ):
        raise MigrationAdmissionError("Saved migration plan is unsafe.")
    raw = _read_expected(path, info.st_size, (info.st_dev, info.st_ino), "migration plan")
    _validate_managed_path(nauro_home(), path)
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ):
        raise MigrationAdmissionError("Saved migration plan changed during inspection.")
    if hashlib.sha256(raw).hexdigest() != record.plan_digest:
        raise MigrationAdmissionError("Saved migration plan differs.")
    return raw


def load_migration_plan(store: Path) -> tuple[MigrationAdmission, bytes]:
    record = inspect_migration(store)
    if record is None:
        raise MigrationAdmissionError("No saved migration plan.")
    return record, _read_plan(record)


def _lock_path(store: Path) -> Path:
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


def save_migration_assessment(plan: LegacyMigrationPlan) -> MigrationAdmission:
    if type(plan) is not LegacyMigrationPlan:
        raise MigrationAdmissionError("A verified preservation plan is required.")
    if len(plan.manifest_json) > MAX_PLAN_BYTES:
        raise MigrationAdmissionError("Migration plan exceeds the local preservation limit.")
    binding = plan.assessment.projection.target.binding
    if binding.mode != "cloud" or binding.server_url is None:
        raise MigrationAdmissionError("Migration requires an existing hosted project.")
    record = MigrationAdmission(
        migration_id=plan.migration_id,
        project_id=binding.project_id,
        actor=plan.assessment.projection.target.identity.installed_for_user_id,
        endpoint=binding.server_url,
        store=str(binding.store_path.absolute()),
        plan_digest=plan.plan_digest,
        phase="assessed",
    )
    home = nauro_home()
    _validate_managed_path(home, home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(_lock_path(binding.store_path), timeout=0):
        prior = inspect_migration(binding.store_path)
        if prior is not None and prior != record:
            raise MigrationAdmissionError(
                "Inspect the retained migration before preparing another."
            )
        path = _plan_path(record)
        _validate_managed_path(home, path)
        try:
            path.lstat()
        except FileNotFoundError:
            atomic_write_bytes(path, plan.manifest_json)
        if _read_plan(record) != plan.manifest_json:
            raise MigrationAdmissionError("Saved migration plan differs.")
        paths = RefreshPaths(home, home)
        sync_file(paths, path)
        sync_parents(paths, home)
        durable_replace(
            paths, admission_path(binding.store_path), record.model_dump_json().encode()
        )
    return record


def decide_migration_assessment(
    expected: MigrationAdmission,
    *,
    preserve: bool,
) -> MigrationAdmission:
    """Record an explicit disposition; this neither copies bytes nor authorizes relocation."""
    if type(expected) is not MigrationAdmission or expected.phase != "assessed":
        raise MigrationAdmissionError("An exact assessed migration is required.")
    store = Path(expected.store)
    desired = expected.model_copy(update={"phase": "blocked" if preserve else "declined"})
    with FileLock(_lock_path(store), timeout=0):
        current = inspect_migration(store)
        if current not in (expected, desired):
            raise MigrationAdmissionError(
                "Migration disposition changed; inspect retained evidence."
            )
        _read_plan(expected)
        paths = RefreshPaths(nauro_home(), nauro_home())
        sync_file(paths, _plan_path(expected))
        sync_parents(paths, nauro_home())
        durable_replace(paths, admission_path(store), desired.model_dump_json().encode())
    return desired
