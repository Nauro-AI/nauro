"""Read-only assessment of a legacy flat store against a verified generation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import suppress
from dataclasses import InitVar, dataclass, field
from pathlib import Path

from nauro_core.protected_generation_membership import is_protected_generation_member

from nauro.store._atomic import is_tmp_sibling
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_projection import VerifiedGenerationProjection
from nauro.store.replica_control import (
    ReplicaControlReadError,
    _is_link_or_reparse,
    _validate_managed_path,
    locked_replica_control_snapshot,
)

_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_CHUNK_BYTES = 1024 * 1024
_PULL_SPOOL_PREFIX = ".pull-spool-"
_SNAPSHOTS_DIRECTORY = "snapshots"
_ASSESSMENT_TOKEN = object()


class LegacyMigrationAssessmentError(GenerationAuthorityError):
    """A legacy store could not be assessed without ambiguous local evidence."""

    code = "legacy_migration_assessment_failed"


@dataclass(frozen=True, order=True)
class LegacyFileStamp:
    """One safely read legacy file represented by path, size, and SHA-256."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, eq=False)
class LegacyMigrationAssessment:
    """One verified projection compared with one read-only legacy inventory."""

    projection: VerifiedGenerationProjection = field(repr=False)
    protected_files: tuple[LegacyFileStamp, ...]
    snapshot_files: tuple[LegacyFileStamp, ...]
    evidence_files: tuple[LegacyFileStamp, ...]
    directory_paths: tuple[str, ...]
    identical_paths: tuple[str, ...]
    divergent_paths: tuple[str, ...]
    local_only_paths: tuple[str, ...]
    server_only_paths: tuple[str, ...]
    pending_paths: tuple[str, ...]
    unsupported_paths: tuple[str, ...]
    capture_digest: str
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if (
            _construction_token is not _ASSESSMENT_TOKEN
            or type(self.projection) is not VerifiedGenerationProjection
        ):
            raise LegacyMigrationAssessmentError(
                "Legacy migration assessments must come from a local store scan."
            )

    @property
    def ready_for_backup(self) -> bool:
        return not self.pending_paths

    @property
    def nonidentical_local_paths(self) -> tuple[str, ...]:
        return tuple(sorted((*self.divergent_paths, *self.local_only_paths)))


def _container_entries(path: Path, *, required: bool, label: str) -> tuple[Path, ...]:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        if required:
            raise LegacyMigrationAssessmentError(f"The legacy {label} is missing.") from None
        return ()
    except OSError as exc:
        raise LegacyMigrationAssessmentError(f"The legacy {label} is unavailable.") from exc
    try:
        unsafe = _is_link_or_reparse(observed)
    except ReplicaControlReadError as exc:
        raise LegacyMigrationAssessmentError(f"The legacy {label} is unsafe.") from exc
    if unsafe or not stat.S_ISDIR(observed.st_mode):
        raise LegacyMigrationAssessmentError(f"The legacy {label} is not a regular directory.")
    try:
        return tuple(sorted(path.iterdir(), key=lambda entry: entry.name))
    except OSError as exc:
        raise LegacyMigrationAssessmentError(f"The legacy {label} cannot be enumerated.") from exc


def _stat_signature(observed: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _stamp_file(store_path: Path, path: Path) -> LegacyFileStamp:
    try:
        return _read_stamp(store_path, path)
    except LegacyMigrationAssessmentError:
        raise
    except (OSError, ValueError, ReplicaControlReadError) as exc:
        raise LegacyMigrationAssessmentError("The legacy file is unreadable.") from exc


def _read_stamp(store_path: Path, path: Path) -> LegacyFileStamp:
    try:
        relative = path.relative_to(store_path).as_posix()
        _validate_managed_path(store_path, path)
        observed = path.lstat()
        unsafe = _is_link_or_reparse(observed)
    except (OSError, ValueError, ReplicaControlReadError) as exc:
        raise LegacyMigrationAssessmentError("A legacy file path is unsafe.") from exc
    if unsafe or not stat.S_ISREG(observed.st_mode):
        raise LegacyMigrationAssessmentError(f"The legacy file is not regular: {relative}.")

    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_signature(opened) != _stat_signature(observed):
            raise LegacyMigrationAssessmentError(
                f"The legacy file changed during open: {relative}."
            )
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            while chunk := handle.read(_READ_CHUNK_BYTES):
                digest.update(chunk)
                total += len(chunk)
            finished = os.fstat(handle.fileno())
        if total != opened.st_size or _stat_signature(finished) != _stat_signature(opened):
            raise LegacyMigrationAssessmentError(
                f"The legacy file changed during read: {relative}."
            )
        rebound = path.lstat()
        if _is_link_or_reparse(rebound) or _stat_signature(rebound) != _stat_signature(opened):
            raise LegacyMigrationAssessmentError(
                f"The legacy file changed during read: {relative}."
            )
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    return LegacyFileStamp(relative, total, digest.hexdigest())


def _inventory(
    store_path: Path,
) -> tuple[tuple[LegacyFileStamp, ...], tuple[str, ...], tuple[str, ...]]:
    files: list[LegacyFileStamp] = []
    directories: list[str] = []
    pending: list[str] = []
    containers = [store_path]
    for container in containers:
        _validate_managed_path(store_path, container)
        before = container.lstat()
        for entry in _container_entries(container, required=True, label="inventory directory"):
            relative = entry.relative_to(store_path).as_posix()
            if relative == ".replica-control.lock":
                continue
            if relative == ".replica":
                raise LegacyMigrationAssessmentError(
                    "Legacy migration assessment requires absent generation authority."
                )
            _validate_managed_path(store_path, entry)
            observed = entry.lstat()
            if is_tmp_sibling(entry.name) or entry.name.startswith(_PULL_SPOOL_PREFIX):
                pending.append(relative)
            if stat.S_ISDIR(observed.st_mode):
                directories.append(relative)
                containers.append(entry)
            else:
                files.append(_stamp_file(store_path, entry))
        _validate_managed_path(store_path, container)
        if _stat_signature(before) != _stat_signature(container.lstat()):
            raise LegacyMigrationAssessmentError("The legacy directory changed during inventory.")
    return tuple(sorted(files)), tuple(sorted(directories)), tuple(sorted(pending))


def _capture_digest(
    projection: VerifiedGenerationProjection,
    protected: tuple[LegacyFileStamp, ...],
    snapshots: tuple[LegacyFileStamp, ...],
    evidence: tuple[LegacyFileStamp, ...],
    directories: tuple[str, ...],
    pending: tuple[str, ...],
    unsupported: tuple[str, ...],
) -> str:
    def stamp_payload(stamp: LegacyFileStamp) -> dict[str, str | int]:
        return {
            "path": stamp.path,
            "size": stamp.size,
            "sha256": stamp.sha256,
        }

    payload = {
        "schema_version": 1,
        "server_url": projection.target.binding.server_url,
        "projection": projection.target.identity.model_dump(),
        "protected": [stamp_payload(stamp) for stamp in protected],
        "snapshots": [stamp_payload(stamp) for stamp in snapshots],
        "evidence": [stamp_payload(stamp) for stamp in evidence],
        "directories": directories,
        "pending": pending,
        "unsupported": unsupported,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_legacy_root(store_path: Path) -> None:
    try:
        (store_path / ".replica").lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LegacyMigrationAssessmentError("Generation authority cannot be inspected.") from exc
    raise LegacyMigrationAssessmentError(
        "Legacy migration assessment requires absent generation authority."
    )


def _require_empty_control_lock(store_path: Path) -> None:
    path = store_path / ".replica-control.lock"
    try:
        _validate_managed_path(store_path, path)
        observed = path.lstat()
    except FileNotFoundError:
        return
    except (OSError, ReplicaControlReadError) as exc:
        raise LegacyMigrationAssessmentError("Legacy control lock cannot be inspected.") from exc
    if not stat.S_ISREG(observed.st_mode) or observed.st_size != 0 or observed.st_nlink != 1:
        raise LegacyMigrationAssessmentError("Legacy control lock contains unsafe evidence.")


def assess_legacy_migration(
    projection: VerifiedGenerationProjection,
    *,
    lock_timeout: float = 0,
) -> LegacyMigrationAssessment:
    """Compare one pre-epoch flat store with verified server projection bytes."""
    if type(projection) is not VerifiedGenerationProjection:
        raise LegacyMigrationAssessmentError(
            "Legacy migration assessment requires a verified projection."
        )
    binding = projection.target.binding
    _require_legacy_root(binding.store_path)
    _require_empty_control_lock(binding.store_path)
    with locked_replica_control_snapshot(
        binding,
        active_user_id=projection.target.identity.installed_for_user_id,
        active_projection_scope_id=projection.target.identity.projection_scope_id,
        timeout=lock_timeout,
    ) as control:
        if control.marker_json is not None:
            raise LegacyMigrationAssessmentError(
                "Legacy migration assessment requires absent generation authority."
            )
        _require_legacy_root(binding.store_path)
        _require_empty_control_lock(binding.store_path)
        try:
            files, directories, pending = _inventory(binding.store_path)
        except (OSError, ReplicaControlReadError) as exc:
            raise LegacyMigrationAssessmentError("The legacy inventory is unavailable.") from exc
        _require_empty_control_lock(binding.store_path)
        protected = tuple(stamp for stamp in files if is_protected_generation_member(stamp.path))
        snapshots = tuple(
            stamp
            for stamp in files
            if stamp.path.startswith("snapshots/") and not stamp.path.endswith(".lock")
        )
        known = {stamp.path for stamp in (*protected, *snapshots)}
        evidence = tuple(stamp for stamp in files if stamp.path not in known)

    local_by_path = {stamp.path: stamp for stamp in protected}
    server_by_path = {artifact.path: artifact.digest for artifact in projection.artifacts}
    shared = set(local_by_path) & set(server_by_path)
    identical = tuple(
        sorted(path for path in shared if local_by_path[path].sha256 == server_by_path[path])
    )
    divergent = tuple(sorted(shared - set(identical)))
    local_only = tuple(sorted(set(local_by_path) - set(server_by_path)))
    server_only = tuple(sorted(set(server_by_path) - set(local_by_path)))
    unsupported = tuple(stamp.path for stamp in evidence)
    capture_digest = _capture_digest(
        projection,
        protected,
        snapshots,
        evidence,
        directories,
        pending,
        unsupported,
    )
    return LegacyMigrationAssessment(
        projection=projection,
        protected_files=protected,
        snapshot_files=snapshots,
        evidence_files=evidence,
        directory_paths=directories,
        identical_paths=identical,
        divergent_paths=divergent,
        local_only_paths=local_only,
        server_only_paths=server_only,
        pending_paths=pending,
        unsupported_paths=unsupported,
        capture_digest=capture_digest,
        _construction_token=_ASSESSMENT_TOKEN,
    )


__all__ = [
    "LegacyFileStamp",
    "LegacyMigrationAssessment",
    "LegacyMigrationAssessmentError",
    "assess_legacy_migration",
]
