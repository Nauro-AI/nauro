"""Persist exact preservation evidence before blocking local admission."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from nauro.store._atomic import atomic_write_bytes
from nauro.store.generation_installation import _read_expected
from nauro.store.generation_migration_assessment import (
    LegacyFileStamp,
    _inventory,
    _require_empty_control_lock,
    _require_legacy_root,
)
from nauro.store.generation_migration_plan import LegacyMigrationPlan
from nauro.store.generation_refresh_io import RefreshPaths, durable_replace, sync_file, sync_parents
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    admission_path,
    inspect_migration,
    migration_home,
    migration_lock,
    same_store_binding,
)
from nauro.store.replica_control import (
    _is_link_or_reparse,
    _read_optional_file,
    _validate_managed_path,
)

MAX_PLAN_BYTES = 16 * 1024 * 1024


def _plan_path(record: MigrationAdmission) -> Path:
    return migration_home() / f"migration-plan-{record.migration_id}.json"


def _read_plan(record: MigrationAdmission) -> bytes:
    path = _plan_path(record)
    _validate_managed_path(migration_home(), path)
    info = path.lstat()
    if (
        _is_link_or_reparse(info)
        or info.st_nlink != 1
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > MAX_PLAN_BYTES
    ):
        raise MigrationAdmissionError("Saved migration plan is unsafe.")
    raw = _read_expected(path, info.st_size, (info.st_dev, info.st_ino), "migration plan")
    _validate_managed_path(migration_home(), path)
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
    inspect_previous_migration(record)
    return record, _read_plan(record)


def _previous_path(digest: str) -> Path:
    return migration_home() / f"migration-history-{digest}.json"


def inspect_previous_migration(record: MigrationAdmission) -> MigrationAdmission | None:
    if record.predecessor_digest is None:
        return None
    path = _previous_path(record.predecessor_digest)
    _validate_managed_path(migration_home(), path)
    raw = _read_optional_file(path)
    if raw is None or hashlib.sha256(raw).hexdigest() != record.predecessor_digest:
        raise MigrationAdmissionError("Migration predecessor evidence differs.")
    previous = MigrationAdmission.model_validate_json(raw)
    _require_replacement_binding(previous, record)
    if previous.canonical_bytes() != raw:
        raise MigrationAdmissionError("Migration predecessor evidence is not canonical.")
    _read_plan(previous)
    return previous


def _require_replacement_binding(previous: MigrationAdmission, desired: MigrationAdmission) -> None:
    if (
        type(previous) is not MigrationAdmission
        or previous.phase not in {"assessed", "declined"}
        or previous.migration_id == desired.migration_id
        or (previous.store, previous.project_id, previous.actor, previous.endpoint)
        != (desired.store, desired.project_id, desired.actor, desired.endpoint)
    ):
        raise MigrationAdmissionError(
            "Migration replacement requires the same binding and inactive evidence."
        )


def _retain_previous(previous: MigrationAdmission, paths: RefreshPaths) -> None:
    _read_plan(previous)
    sync_file(paths, _plan_path(previous))
    raw = previous.canonical_bytes()
    path = _previous_path(hashlib.sha256(raw).hexdigest())
    _validate_managed_path(paths.store, path)
    existing = _read_optional_file(path)
    if existing is not None and existing != raw:
        raise MigrationAdmissionError("Migration predecessor evidence differs.")
    if existing is None:
        durable_replace(paths, path, raw)
    sync_file(paths, path)
    sync_parents(paths, paths.store)


def save_migration_assessment(
    plan: LegacyMigrationPlan,
    *,
    replace: MigrationAdmission | None = None,
) -> MigrationAdmission:
    if type(plan) is not LegacyMigrationPlan:
        raise MigrationAdmissionError("A verified preservation plan is required.")
    if len(plan.manifest_json) > MAX_PLAN_BYTES:
        raise MigrationAdmissionError("Migration plan exceeds the local preservation limit.")
    binding = plan.assessment.projection.target.binding
    if binding.mode != "cloud" or binding.server_url is None:
        raise MigrationAdmissionError("Migration requires an existing hosted project.")
    if replace is not None and (
        type(replace) is not MigrationAdmission
        or not same_store_binding(Path(replace.store), binding.store_path)
    ):
        raise MigrationAdmissionError("Migration replacement requires the same physical store.")
    record = MigrationAdmission(
        migration_id=plan.migration_id,
        project_id=binding.project_id,
        actor=plan.assessment.projection.target.identity.installed_for_user_id,
        endpoint=binding.server_url,
        store=(
            replace.store
            if type(replace) is MigrationAdmission
            else str(binding.store_path.resolve())
        ),
        plan_digest=plan.plan_digest,
        predecessor_digest=(
            hashlib.sha256(replace.canonical_bytes()).hexdigest()
            if type(replace) is MigrationAdmission
            else None
        ),
        phase="assessed",
    )
    if replace is not None:
        _require_replacement_binding(replace, record)
    home = migration_home()
    _validate_managed_path(home, home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with migration_lock(binding.store_path):
        prior = inspect_migration(binding.store_path)
        if prior is not None and prior.model_copy(update={"store": record.store}) == record:
            record = prior
        if prior != record and (prior is not None or replace is not None) and prior != replace:
            raise MigrationAdmissionError(
                "Inspect the retained migration before preparing another."
            )
        if replace is not None:
            _retain_previous(replace, RefreshPaths(home, home))
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
        durable_replace(paths, admission_path(binding.store_path), record.canonical_bytes())
    return record


def verify_migration_source(record: MigrationAdmission, raw_plan: bytes) -> None:
    try:
        _compare_source(record, raw_plan)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MigrationAdmissionError(
            "Project files changed or are unavailable; prepare and confirm a fresh assessment."
        ) from exc


def _compare_source(record: MigrationAdmission, raw_plan: bytes) -> None:
    store = Path(record.store)
    payload = json.loads(raw_plan)
    if (
        payload["migration_id"] != record.migration_id
        or payload["project_id"] != record.project_id
        or payload["server_url"] != record.endpoint
        or payload["projection"]["installed_for_user_id"] != record.actor
    ):
        raise ValueError("Saved plan binding differs")
    expected = tuple(
        sorted(
            LegacyFileStamp(entry["source_path"], entry["size"], entry["sha256"])
            for entry in payload["entries"]
        )
    )
    _require_legacy_root(store)
    _require_empty_control_lock(store)
    files, directories, pending = _inventory(store)
    _require_empty_control_lock(store)
    _require_legacy_root(store)
    if pending or files != expected or directories != tuple(payload["directory_paths"]):
        raise ValueError("Source inventory differs")


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
    with migration_lock(store):
        current = inspect_migration(store)
        if current not in (expected, desired):
            raise MigrationAdmissionError(
                "Migration disposition changed; inspect retained evidence."
            )
        raw_plan = _read_plan(expected)
        inspect_previous_migration(expected)
        if preserve and current == expected:
            verify_migration_source(expected, raw_plan)
        paths = RefreshPaths(migration_home(), migration_home())
        sync_file(paths, _plan_path(expected))
        sync_parents(paths, migration_home())
        durable_replace(paths, admission_path(store), desired.canonical_bytes())
    return desired
