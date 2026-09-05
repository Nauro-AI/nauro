"""Stage and audit immutable generation roots under the replica control directory."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nauro.auth import ActiveUserReadError, read_active_user_id
from nauro.store._atomic import atomic_write_bytes, is_tmp_sibling
from nauro.store.generation_authority import (
    FlatProjectAuthority,
    GenerationAuthorityError,
    GenerationAuthorityMarker,
    GenerationControlCorruptError,
    GenerationProjectAuthority,
    InstalledAuthorizationView,
    InstalledGenerationPointer,
    RefreshRequiredError,
    ReplicaActorMismatchError,
    _parse_authorization_view,
    _parse_pointer,
    _PendingGenerationAuthority,
    _select_marker_authority,
    select_project_authority,
)
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    GenerationProjectionVerificationError,
    VerifiedGenerationProjection,
    _parse_manifest,
    _target_parts,
)
from nauro.store.replica_control import (
    _READ_FLAGS,
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
    ReplicaControlReadError,
    _actor_authorization_view,
    _is_link_or_reparse,
    _native_control_lock,
    _read_actor_authorization_view,
    _read_optional_file,
    _validate_managed_path,
    _validate_store_path,
)
from nauro.store.repo_config import generate_ulid
from nauro.sync._path_diagnostics import _escape_path_for_display

_GENERATIONS_DIR = "generations"
_STAGING_DIR = "staging"
_STORE_DIR = "store"
_MANIFEST_NAME = "manifest.json"
_ROOT_KEY_HEX = 32
_STAGING_TOKEN_BYTES = 8
_NOT_DIRECTORY = "The generation root component is not a directory."
_LAYOUT_FAILED = "The generation root layout could not be prepared."
_PUBLISH_FAILED = "The generation root could not be published."
_OCCUPIED = "The generation root is occupied by a non-directory entry."
_STALE_STAGING_SECONDS = 3600
_AUTHORITY_MARKER_NAME = "authority.json"
_POINTER_NAME = "pointer.json"
_INSTALLED_FIELDS = frozenset({"target", "root_key", "root_path", "audit", "reused"})
_AUDIT_FIELDS = frozenset({"manifest_digest", "artifact_count", "byte_total"})
_TARGET_POINTER_FIELDS = (
    "project_id store_format_version generation_id manifest_digest committed_at "
    "installed_for_user_id projection_class projection_scope_id"
).split()
_NON_ACTOR_FIELDS = tuple(
    field for field in _TARGET_POINTER_FIELDS if field != "installed_for_user_id"
)
_ACTOR_UNAVAILABLE = "The installed replica does not match an active account."
_ACTOR_DIFFERENT = "The installed replica belongs to another account."
_AUTHORIZATION_INVALID = "The installed authorization view is invalid."
_POINTER_INVALID = "The installed generation pointer is invalid."
_REFRESH_REQUIRED = "The installed replica requires a fresh authorization view."


class GenerationInstallError(GenerationAuthorityError):
    code = "generation_install_failed"


class GenerationRootDivergedError(GenerationAuthorityError):
    code = "generation_root_diverged"


class GenerationControlPublicationError(GenerationAuthorityError):
    code = "generation_control_publication_failed"


@dataclass(frozen=True)
class GenerationRootAudit:
    manifest_digest: str
    artifact_count: int
    byte_total: int


@dataclass(frozen=True)
class StagedGenerationRoot:
    target: GenerationProjectionTarget
    root_key: str
    root_path: Path
    staging_path: Path
    audit: GenerationRootAudit


@dataclass(frozen=True)
class InstalledGenerationRoot:
    target: GenerationProjectionTarget
    root_key: str
    root_path: Path
    audit: GenerationRootAudit
    reused: bool


@dataclass(frozen=True)
class _GenerationLayout:
    control_root: Path
    actor: Path
    generations: Path
    staging_dir: Path
    root_key: str
    root_path: Path


def _root_key(target: GenerationProjectionTarget) -> str:
    identity = target.identity
    facts = {
        "generation_id": identity.generation_id,
        "projection_class": identity.projection_class,
        "projection_scope_id": identity.projection_scope_id,
    }
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:_ROOT_KEY_HEX]


def _layout(store_path: Path, target: GenerationProjectionTarget) -> _GenerationLayout:
    identity = target.identity
    control_root = store_path / _REPLICA_CONTROL_ROOT_NAME
    version = f"v{identity.store_format_version}"
    actor = control_root / version / "actors" / identity.installed_for_user_id
    generations = actor / _GENERATIONS_DIR
    root_key = _root_key(target)
    return _GenerationLayout(
        control_root, actor, generations, actor / _STAGING_DIR, root_key, generations / root_key
    )


def _probe(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise GenerationInstallError(_LAYOUT_FAILED) from exc


def _create_component(store_path: Path, path: Path) -> os.stat_result:
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    except OSError as exc:
        raise GenerationInstallError(_LAYOUT_FAILED) from exc
    _validate_managed_path(store_path, path)
    return _probe(path)


def _ensure_directory(store_path: Path, path: Path) -> None:
    _validate_managed_path(store_path, path)
    current = store_path
    for part in path.relative_to(store_path).parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            metadata = _create_component(store_path, current)
        except OSError as exc:
            raise GenerationInstallError(_LAYOUT_FAILED) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise GenerationInstallError(_NOT_DIRECTORY)


def _create_staging(store_path: Path, layout: _GenerationLayout) -> Path:
    while True:
        token = secrets.token_hex(_STAGING_TOKEN_BYTES)
        staging = layout.staging_dir / f"{layout.root_key}-{token}"
        try:
            os.mkdir(staging)
        except FileExistsError:
            continue
        except OSError as exc:
            raise GenerationInstallError(_LAYOUT_FAILED) from exc
        _validate_managed_path(store_path, staging)
        if not stat.S_ISDIR(_probe(staging).st_mode):
            raise GenerationInstallError(_NOT_DIRECTORY)
        return staging


def _discard_staging(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    with suppress(OSError):
        os.lstat(staging)


def _stage_tree(store_path: Path, staging: Path, projection: VerifiedGenerationProjection) -> None:
    _ensure_directory(store_path, staging / _STORE_DIR)
    entries = [(f"{_STORE_DIR}/{item.path}", item.content) for item in projection.artifacts]
    entries.append((_MANIFEST_NAME, projection.manifest_json))
    for relative, content in entries:
        target = staging / relative
        _ensure_directory(store_path, target.parent)
        _validate_managed_path(store_path, target)
        try:
            atomic_write_bytes(target, content)
        except OSError as exc:
            _discard_staging(staging)
            raise GenerationInstallError(
                f"The generation artifact could not be staged: {relative}."
            ) from exc


def _expected_entries(
    projection: VerifiedGenerationProjection,
) -> tuple[dict[str, int], set[str]]:
    files = {_MANIFEST_NAME: len(projection.manifest_json)}
    directories = {_STORE_DIR}
    for artifact in projection.artifacts:
        relative = f"{_STORE_DIR}/{artifact.path}"
        files[relative] = len(artifact.content)
        parts = relative.split("/")
        directories.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    return files, directories


def _walk_tree(
    directory: Path, files: dict[str, int], directories: set[str]
) -> dict[str, tuple[int, int]]:
    # The sync admission walker is not reused here: it admits a link whose target is a
    # regular file inside the store, and this audit must refuse every link.
    observed: dict[str, tuple[int, int]] = {}
    seen: set[str] = set()
    frames = [""]
    while frames:
        prefix = frames.pop()
        try:
            with os.scandir(directory / prefix) as entries:
                children = [(entry.name, entry.path) for entry in entries]
        except OSError as exc:
            display = _escape_path_for_display(prefix or ".")
            raise GenerationInstallError(f"The generation root is unreadable: {display}.") from exc
        for name, native in children:
            relative = f"{prefix}/{name}" if prefix else name
            display = _escape_path_for_display(relative)
            try:
                metadata = os.stat(native, follow_symlinks=False)
            except OSError as exc:
                raise GenerationInstallError(
                    f"The generation root is unreadable: {display}."
                ) from exc
            if _is_link_or_reparse(metadata):
                raise GenerationInstallError(f"The generation root holds a link: {display}.")
            if is_tmp_sibling(name):
                raise GenerationInstallError(
                    f"The generation root holds a partial write: {display}."
                )
            is_directory = stat.S_ISDIR(metadata.st_mode)
            if not is_directory and not stat.S_ISREG(metadata.st_mode):
                raise GenerationInstallError(
                    f"The generation root holds an irregular entry: {display}."
                )
            if relative not in (directories if is_directory else files):
                raise GenerationInstallError(
                    f"The generation root holds an unexpected entry: {display}."
                )
            if is_directory:
                seen.add(relative)
                frames.append(relative)
                continue
            if metadata.st_nlink > 1:
                raise GenerationInstallError(f"The generation root holds a linked file: {display}.")
            observed[relative] = (metadata.st_dev, metadata.st_ino)
    for relative in sorted(directories | files.keys()):
        if relative not in seen and relative not in observed:
            raise GenerationInstallError(f"The generation root is missing an entry: {relative}.")
    return observed


def _read_expected(path: Path, length: int, identity: tuple[int, int], display: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        opened = os.fstat(descriptor)
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != identity
        ):
            raise GenerationInstallError(f"The generation root changed during audit: {display}.")
        content = bytearray()
        while len(content) <= length:
            chunk = os.read(descriptor, length + 1 - len(content))
            if not chunk:
                break
            content += chunk
        return bytes(content)
    except OSError as exc:
        raise GenerationInstallError(f"The generation root is unreadable: {display}.") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def audit_generation_tree(
    directory: Path, projection: VerifiedGenerationProjection
) -> GenerationRootAudit:
    files, directories = _expected_entries(projection)
    observed = _walk_tree(directory, files, directories)
    manifest = _read_expected(
        directory / _MANIFEST_NAME,
        files[_MANIFEST_NAME],
        observed[_MANIFEST_NAME],
        _escape_path_for_display(_MANIFEST_NAME),
    )
    manifest_digest = hashlib.sha256(manifest).hexdigest()
    if (
        manifest != projection.manifest_json
        or manifest_digest != projection.target.identity.manifest_digest
    ):
        raise GenerationInstallError("The generation root manifest diverges.")
    byte_total = len(manifest)
    for artifact in projection.artifacts:
        relative = f"{_STORE_DIR}/{artifact.path}"
        content = _read_expected(
            directory / relative,
            files[relative],
            observed[relative],
            _escape_path_for_display(relative),
        )
        if hashlib.sha256(content).hexdigest() != projection.manifest.artifacts[artifact.path]:
            raise GenerationInstallError(f"The generation artifact diverges: {artifact.path}.")
        byte_total += len(content)
    return GenerationRootAudit(manifest_digest, len(projection.artifacts), byte_total)


def _prepare_layout(
    projection: VerifiedGenerationProjection,
) -> tuple[Path, _GenerationLayout]:
    if type(projection) is not VerifiedGenerationProjection:
        raise GenerationInstallError("Generation installation requires a verified projection.")
    store_path = _validate_store_path(projection.target.binding)
    layout = _layout(store_path, projection.target)
    for directory in (
        layout.control_root,
        layout.actor.parent.parent,
        layout.actor.parent,
        layout.actor,
        layout.generations,
        layout.staging_dir,
    ):
        _ensure_directory(store_path, directory)
    return store_path, layout


def _stage(
    store_path: Path, layout: _GenerationLayout, projection: VerifiedGenerationProjection
) -> StagedGenerationRoot:
    staging = _create_staging(store_path, layout)
    _stage_tree(store_path, staging, projection)
    try:
        audit = audit_generation_tree(staging, projection)
    except GenerationInstallError:
        _discard_staging(staging)
        raise
    return StagedGenerationRoot(
        projection.target, layout.root_key, layout.root_path, staging, audit
    )


def stage_generation_root(projection: VerifiedGenerationProjection) -> StagedGenerationRoot:
    store_path, layout = _prepare_layout(projection)
    return _stage(store_path, layout, projection)


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GenerationInstallError(_LAYOUT_FAILED) from exc


def _sweep_stale_staging(staging_dir: Path) -> None:
    try:
        with os.scandir(staging_dir) as entries:
            listed = [(entry.path, entry.stat(follow_symlinks=False)) for entry in entries]
    except OSError:
        return
    cutoff = time.time() - _STALE_STAGING_SECONDS
    for native, metadata in listed:
        if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            continue
        if metadata.st_mtime < cutoff:
            _discard_staging(Path(native))


def _rename_into_place(staging: Path, root_path: Path) -> None:
    try:
        os.replace(staging, root_path)
        metadata = os.lstat(root_path)
    except OSError as exc:
        raise GenerationInstallError(_PUBLISH_FAILED) from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise GenerationInstallError(_PUBLISH_FAILED)


def _publish_or_reuse(
    store_path: Path,
    layout: _GenerationLayout,
    staged: StagedGenerationRoot | None,
    projection: VerifiedGenerationProjection,
    lock_path: Path,
    timeout: float,
) -> tuple[GenerationRootAudit, bool]:
    with _native_control_lock(store_path, lock_path, timeout):
        _validate_managed_path(store_path, layout.root_path)
        if staged is not None:
            _validate_managed_path(store_path, staged.staging_path)
        metadata = _lstat_optional(layout.root_path)
        if metadata is None:
            if staged is None:
                raise GenerationInstallError(_PUBLISH_FAILED)
            _rename_into_place(staged.staging_path, layout.root_path)
            return staged.audit, False
        if not stat.S_ISDIR(metadata.st_mode):
            raise GenerationRootDivergedError(_OCCUPIED)
        try:
            return audit_generation_tree(layout.root_path, projection), True
        except GenerationInstallError as exc:
            raise GenerationRootDivergedError(str(exc)) from None


def install_generation_root(
    projection: VerifiedGenerationProjection, *, timeout: float = -1
) -> InstalledGenerationRoot:
    store_path, layout = _prepare_layout(projection)
    lock_path = store_path / _REPLICA_CONTROL_LOCK_NAME
    _validate_managed_path(store_path, lock_path)
    _sweep_stale_staging(layout.staging_dir)
    _validate_managed_path(store_path, layout.root_path)
    staged: StagedGenerationRoot | None = None
    if _lstat_optional(layout.root_path) is None:
        staged = _stage(store_path, layout, projection)
    try:
        audit, reused = _publish_or_reuse(
            store_path, layout, staged, projection, lock_path, timeout
        )
    except GenerationAuthorityError:
        if staged is not None:
            _discard_staging(staged.staging_path)
        raise
    if reused and staged is not None:
        _discard_staging(staged.staging_path)
    return InstalledGenerationRoot(
        projection.target, layout.root_key, layout.root_path, audit, reused
    )


def _publication_failure() -> GenerationControlPublicationError:
    return GenerationControlPublicationError("The generation control state could not be published.")


def _exact_state(
    value: object, expected_type: type[object], fields: frozenset[str]
) -> dict[str, object]:
    try:
        state = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        state = None
    if (
        type(value) is not expected_type
        or type(state) is not dict
        or not all(type(key) is str for key in state)
        or set(state) != fields
    ):
        raise _publication_failure()
    return state


def _rebuild_installed_root(value: object) -> InstalledGenerationRoot:
    state = _exact_state(value, InstalledGenerationRoot, _INSTALLED_FIELDS)
    try:
        binding, identity = _target_parts(state["target"])
    except GenerationProjectionVerificationError as exc:
        raise _publication_failure() from exc
    target = GenerationProjectionTarget(binding, identity)
    audit_state = _exact_state(state["audit"], GenerationRootAudit, _AUDIT_FIELDS)
    digest = audit_state["manifest_digest"]
    count = audit_state["artifact_count"]
    total = audit_state["byte_total"]
    root_key = state["root_key"]
    root_path = state["root_path"]
    reused = state["reused"]
    if (
        type(digest) is not str
        or type(count) is not int
        or count < 0
        or type(total) is not int
        or total < 0
        or type(root_key) is not str
        or type(root_path) is not type(Path())
        or type(reused) is not bool
    ):
        raise _publication_failure()
    return InstalledGenerationRoot(
        target, root_key, root_path, GenerationRootAudit(digest, count, total), reused
    )


def _derive_publication(value: object) -> tuple[InstalledGenerationRoot, Path, _GenerationLayout]:
    installed = _rebuild_installed_root(value)
    store_path = _validate_store_path(installed.target.binding)
    layout = _layout(store_path, installed.target)
    if installed.root_key != layout.root_key or installed.root_path != layout.root_path:
        raise _publication_failure()
    return installed, store_path, layout


def _require_directory(store_path: Path, path: Path, *, root: bool = False) -> None:
    _validate_managed_path(store_path, path)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if root:
            raise GenerationRootDivergedError("The generation root is missing.") from None
        raise ReplicaControlReadError("Replica control path metadata is unavailable.") from None
    except OSError as exc:
        raise ReplicaControlReadError("Replica control path metadata is unavailable.") from exc
    if _is_link_or_reparse(metadata):
        raise ReplicaControlReadError("Replica control paths cannot contain links.")
    if not stat.S_ISDIR(metadata.st_mode):
        if root:
            raise GenerationRootDivergedError(_OCCUPIED)
        raise ReplicaControlReadError("Replica control path is not a plain directory.")


def _validate_lock_path(store_path: Path, path: Path, *, required: bool) -> None:
    _validate_managed_path(store_path, path)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if not required:
            return
        raise ReplicaControlReadError("Replica control lock is unavailable.") from None
    except OSError as exc:
        raise ReplicaControlReadError("Replica control lock is unavailable.") from exc
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ReplicaControlReadError("Replica control lock path is unsafe.")


def _reprove_publication_paths(
    installed: InstalledGenerationRoot,
    store_path: Path,
    layout: _GenerationLayout,
    lock_path: Path,
) -> None:
    _validate_store_path(installed.target.binding)
    _validate_lock_path(store_path, lock_path, required=True)
    for directory in (
        layout.control_root,
        layout.actor.parent.parent,
        layout.actor.parent,
        layout.actor,
        layout.generations,
    ):
        _require_directory(store_path, directory)
    _require_directory(store_path, layout.root_path, root=True)


def _read_control_file(store_path: Path, path: Path) -> bytes | None:
    _require_directory(store_path, path.parent)
    _validate_managed_path(store_path, path)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReplicaControlReadError("Replica control file metadata is unavailable.") from exc
    if before.st_nlink != 1:
        raise ReplicaControlReadError("Replica control file is not a bounded regular file.")
    content = _read_optional_file(path)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise ReplicaControlReadError("Replica control file changed during read.") from exc
    if (
        _is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (after.st_dev, after.st_ino, after.st_size)
        != (before.st_dev, before.st_ino, before.st_size)
    ):
        raise ReplicaControlReadError("Replica control file changed during read.")
    return content


def _installed_file(path: Path, display: str) -> tuple[tuple[int, int], int]:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        raise GenerationInstallError(
            f"The generation root is missing an entry: {display}."
        ) from None
    except OSError as exc:
        raise GenerationInstallError(f"The generation root is unreadable: {display}.") from exc
    if _is_link_or_reparse(metadata):
        raise GenerationInstallError(f"The generation root holds a link: {display}.")
    if not stat.S_ISREG(metadata.st_mode):
        raise GenerationInstallError(f"The generation root holds an irregular entry: {display}.")
    if metadata.st_nlink > 1:
        raise GenerationInstallError(f"The generation root holds a linked file: {display}.")
    return (metadata.st_dev, metadata.st_ino), metadata.st_size


def _stream_digest(
    path: Path,
    identity: tuple[int, int],
    size: int,
    expected: str,
    display: str,
    mismatch: str,
) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        opened = os.fstat(descriptor)
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != identity
            or opened.st_size != size
        ):
            raise GenerationInstallError(f"The generation root changed during audit: {display}.")
        hashed, total = hashlib.sha256(), 0
        while chunk := os.read(descriptor, 64 * 1024):
            hashed.update(chunk)
            total += len(chunk)
        finished = os.fstat(descriptor)
        if (
            _is_link_or_reparse(finished)
            or not stat.S_ISREG(finished.st_mode)
            or finished.st_nlink != 1
            or (finished.st_dev, finished.st_ino) != identity
            or finished.st_size != size
            or total != size
        ):
            raise GenerationInstallError(f"The generation root changed during audit: {display}.")
        if hashed.hexdigest() != expected:
            raise GenerationInstallError(mismatch)
        return total
    except OSError as exc:
        raise GenerationInstallError(f"The generation root is unreadable: {display}.") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _audit_installed_root(
    store_path: Path, layout: _GenerationLayout, installed: InstalledGenerationRoot
) -> str:
    _require_directory(store_path, layout.root_path, root=True)
    try:
        manifest_path = layout.root_path / _MANIFEST_NAME
        manifest_identity, manifest_size = _installed_file(manifest_path, _MANIFEST_NAME)
        _stream_digest(
            manifest_path,
            manifest_identity,
            manifest_size,
            installed.target.identity.manifest_digest,
            _MANIFEST_NAME,
            "The generation root manifest diverges.",
        )
        manifest_json = _read_expected(
            manifest_path, manifest_size, manifest_identity, _MANIFEST_NAME
        )
        if len(manifest_json) != manifest_size or _installed_file(
            manifest_path, _MANIFEST_NAME
        ) != (manifest_identity, manifest_size):
            raise GenerationInstallError("The generation root changed during audit: manifest.json.")
        manifest = _parse_manifest(installed.target, manifest_json)
        files = {_MANIFEST_NAME: len(manifest_json)}
        directories = {_STORE_DIR}
        for artifact in manifest.artifacts:
            relative = f"{_STORE_DIR}/{artifact}"
            files[relative] = 0
            parts = relative.split("/")
            directories.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
        observed = _walk_tree(layout.root_path, files, directories)
        if observed[_MANIFEST_NAME] != manifest_identity:
            raise GenerationInstallError("The generation root changed during audit: manifest.json.")
        byte_total = len(manifest_json)
        for artifact, digest in manifest.artifacts.items():
            relative = f"{_STORE_DIR}/{artifact}"
            display = _escape_path_for_display(artifact)
            identity, size = _installed_file(layout.root_path / relative, display)
            if identity != observed[relative]:
                raise GenerationInstallError(
                    f"The generation root changed during audit: {display}."
                )
            byte_total += _stream_digest(
                layout.root_path / relative,
                identity,
                size,
                digest,
                display,
                f"The generation artifact diverges: {display}.",
            )
        audit = GenerationRootAudit(
            installed.target.identity.manifest_digest, len(manifest.artifacts), byte_total
        )
    except (GenerationInstallError, GenerationProjectionVerificationError) as exc:
        raise GenerationRootDivergedError(str(exc)) from None
    if audit != installed.audit:
        raise _publication_failure()
    return manifest.committed_at


def _expected_marker(target: GenerationProjectionTarget) -> GenerationAuthorityMarker:
    identity = target.identity
    return GenerationAuthorityMarker.model_validate(
        {
            "schema_version": 1,
            "authority": "generation",
            "project_id": identity.project_id,
            "store_format_version": identity.store_format_version,
        }
    )


def _require_target(
    authority: GenerationProjectAuthority, target: GenerationProjectionTarget
) -> None:
    pointer, identity = authority.pointer, target.identity
    if any(getattr(pointer, field) != getattr(identity, field) for field in _TARGET_POINTER_FIELDS):
        raise _publication_failure()


def _require_active_actor(expected: str) -> str:
    try:
        actor = read_active_user_id()
    except ActiveUserReadError:
        raise ReplicaActorMismatchError(_ACTOR_UNAVAILABLE) from None
    if actor != expected:
        raise ReplicaActorMismatchError(_ACTOR_DIFFERENT) from None
    return actor


def _validate_carrier(
    target: GenerationProjectionTarget,
    active_actor: str,
    marker_json: bytes | None,
    carrier: InstalledAuthorizationView,
) -> bool:
    identity = target.identity
    if (
        carrier.installed_for_user_id != active_actor
        or carrier.installed_for_user_id != identity.installed_for_user_id
    ):
        raise ReplicaActorMismatchError(_ACTOR_DIFFERENT)
    exact = all(getattr(carrier, field) == getattr(identity, field) for field in _NON_ACTOR_FIELDS)
    if marker_json is not None and not exact:
        raise RefreshRequiredError(_REFRESH_REQUIRED)
    return exact


def _validate_control_pair(
    target: GenerationProjectionTarget,
    active_actor: str,
    marker_json: bytes | None,
    carrier: InstalledAuthorizationView,
    pointer: InstalledGenerationPointer,
) -> bool:
    carrier_exact = _validate_carrier(target, active_actor, marker_json, carrier)
    identity = target.identity
    if (
        pointer.installed_for_user_id != active_actor
        or pointer.installed_for_user_id != identity.installed_for_user_id
        or pointer.installed_for_user_id != carrier.installed_for_user_id
    ):
        if marker_json is not None:
            raise ReplicaActorMismatchError(_ACTOR_DIFFERENT)
        return False
    exact = carrier_exact and all(
        getattr(pointer, field) == getattr(identity, field) == getattr(carrier, field)
        for field in _NON_ACTOR_FIELDS
    )
    exact = exact and pointer.installed_state_id == carrier.installed_state_id
    if marker_json is not None and not exact:
        raise RefreshRequiredError(_REFRESH_REQUIRED)
    return exact


def _projection_fields(
    target: GenerationProjectionTarget, active_actor: str, committed_at: str
) -> dict[str, object]:
    identity = target.identity
    return {
        field: (
            active_actor
            if field == "installed_for_user_id"
            else committed_at
            if field == "committed_at"
            else getattr(identity, field)
        )
        for field in _TARGET_POINTER_FIELDS
    }


def _build_carrier(
    target: GenerationProjectionTarget,
    active_actor: str,
    committed_at: str,
    installed_state_id: str,
) -> InstalledAuthorizationView:
    return InstalledAuthorizationView.model_validate(
        {
            "schema_version": 1,
            **_projection_fields(target, active_actor, committed_at),
            "installed_state_id": installed_state_id,
        }
    )


def _build_pointer(
    target: GenerationProjectionTarget,
    active_actor: str,
    committed_at: str,
    installed_at: str,
    installed_state_id: str,
) -> InstalledGenerationPointer:
    return InstalledGenerationPointer.model_validate(
        {
            "schema_version": 1,
            **_projection_fields(target, active_actor, committed_at),
            "installed_at": installed_at,
            "installed_state_id": installed_state_id,
        }
    )


def _parse_exact_pointer(raw: bytes) -> InstalledGenerationPointer:
    try:
        pointer = _parse_pointer(raw)
    except GenerationControlCorruptError:
        raise GenerationControlCorruptError(_POINTER_INVALID) from None
    if raw != pointer.canonical_bytes():
        raise GenerationControlCorruptError(_POINTER_INVALID)
    return pointer


def _require_published_carrier(
    target: GenerationProjectionTarget,
    active_actor: str,
    raw: bytes,
    intended: bytes,
) -> InstalledAuthorizationView:
    try:
        carrier = _parse_authorization_view(raw)
    except GenerationControlCorruptError:
        raise GenerationControlCorruptError(_AUTHORIZATION_INVALID) from None
    if raw != carrier.canonical_bytes():
        raise GenerationControlCorruptError(_AUTHORIZATION_INVALID)
    exact = _validate_carrier(target, active_actor, None, carrier)
    if raw != intended or not exact:
        raise _publication_failure()
    return carrier


def _require_published_pair(
    target: GenerationProjectionTarget,
    active_actor: str,
    carrier: InstalledAuthorizationView,
    pointer: InstalledGenerationPointer,
) -> None:
    identity = target.identity
    if (
        carrier.installed_for_user_id != active_actor
        or pointer.installed_for_user_id != active_actor
        or identity.installed_for_user_id != active_actor
    ):
        raise ReplicaActorMismatchError(_ACTOR_DIFFERENT)
    if not _validate_control_pair(target, active_actor, None, carrier, pointer):
        raise _publication_failure()


def _select_exact(
    target: GenerationProjectionTarget, marker_json: bytes, pointer_json: bytes | None
) -> GenerationProjectAuthority:
    selected = select_project_authority(
        target.binding,
        marker_json=marker_json,
        pointer_json=pointer_json,
        active_user_id=target.identity.installed_for_user_id,
        active_projection_scope_id=target.identity.projection_scope_id,
    )
    if not isinstance(selected, GenerationProjectAuthority):
        raise _publication_failure()
    if (
        marker_json != selected.marker.canonical_bytes()
        or pointer_json != selected.pointer.canonical_bytes()
    ):
        raise GenerationControlCorruptError("Generation control state is not canonical.")
    _require_target(selected, target)
    return selected


def _read_active_marker(target: GenerationProjectionTarget, marker_json: bytes) -> None:
    selected = _select_marker_authority(
        target.binding,
        marker_json=marker_json,
        active_user_id=target.identity.installed_for_user_id,
    )
    if (
        isinstance(selected, FlatProjectAuthority)
        or marker_json != selected.marker.canonical_bytes()
    ):
        raise GenerationControlCorruptError("The generation authority marker is not canonical.")


def _active_selection(
    target: GenerationProjectionTarget,
    store_path: Path,
    pending: _PendingGenerationAuthority,
    pointer_path: Path,
    marker_json: bytes,
) -> tuple[GenerationProjectAuthority, bytes, bytes]:
    _read_active_marker(target, marker_json)
    carrier_json, carrier = _read_actor_authorization_view(pending)
    if carrier_json is None or carrier is None:
        raise RefreshRequiredError(_REFRESH_REQUIRED)
    _validate_carrier(target, pending.active_user_id, marker_json, carrier)
    pointer_json = _read_control_file(store_path, pointer_path)
    if pointer_json is None:
        raise RefreshRequiredError("The active account has no installed generation pointer.")
    pointer = _parse_exact_pointer(pointer_json)
    _validate_control_pair(target, pending.active_user_id, marker_json, carrier, pointer)
    return _select_exact(target, marker_json, pointer_json), carrier_json, pointer_json


def _write_carrier(
    installed: InstalledGenerationRoot,
    store_path: Path,
    layout: _GenerationLayout,
    lock_path: Path,
    marker_path: Path,
    carrier_path: Path,
    active_actor: str,
    prior: bytes | None,
    intended: bytes,
) -> InstalledAuthorizationView:
    _reprove_publication_paths(installed, store_path, layout, lock_path)
    if _read_control_file(store_path, carrier_path) != prior:
        raise _publication_failure()
    if _read_control_file(store_path, marker_path) is not None:
        raise _publication_failure()
    _require_published_carrier(installed.target, active_actor, intended, intended)
    _require_active_actor(active_actor)
    failure: Exception | None = None
    try:
        atomic_write_bytes(carrier_path, intended)
    except Exception as exc:
        failure = exc
    observed = _read_control_file(store_path, carrier_path)
    if observed != intended:
        raise _publication_failure() from None
    carrier = _require_published_carrier(installed.target, active_actor, observed, intended)
    if failure is not None:
        _require_active_actor(active_actor)
    return carrier


def _write_pointer(
    installed: InstalledGenerationRoot,
    store_path: Path,
    layout: _GenerationLayout,
    lock_path: Path,
    marker_path: Path,
    carrier_path: Path,
    pointer_path: Path,
    active_actor: str,
    carrier_bytes: bytes,
    prior: bytes | None,
    intended: bytes,
) -> InstalledGenerationPointer:
    _reprove_publication_paths(installed, store_path, layout, lock_path)
    observed_carrier = _read_control_file(store_path, carrier_path)
    if observed_carrier != carrier_bytes:
        raise _publication_failure()
    carrier = _require_published_carrier(
        installed.target, active_actor, observed_carrier, carrier_bytes
    )
    if _read_control_file(store_path, pointer_path) != prior:
        raise _publication_failure()
    if _read_control_file(store_path, marker_path) is not None:
        raise _publication_failure()
    intended_pointer = _parse_exact_pointer(intended)
    _require_published_pair(installed.target, active_actor, carrier, intended_pointer)
    _require_active_actor(active_actor)
    failure: Exception | None = None
    try:
        atomic_write_bytes(pointer_path, intended)
    except Exception as exc:
        failure = exc
    observed_carrier = _read_control_file(store_path, carrier_path)
    observed_pointer = _read_control_file(store_path, pointer_path)
    if observed_carrier is None or observed_pointer != intended:
        raise _publication_failure() from None
    carrier = _require_published_carrier(
        installed.target, active_actor, observed_carrier, carrier_bytes
    )
    pointer = _parse_exact_pointer(observed_pointer)
    _require_published_pair(installed.target, active_actor, carrier, pointer)
    if failure is not None:
        _require_active_actor(active_actor)
    return pointer


def _publish_generation_control(
    installed: InstalledGenerationRoot, timeout: float
) -> GenerationProjectAuthority:
    rebuilt = _rebuild_installed_root(installed)
    active_actor = _require_active_actor(rebuilt.target.identity.installed_for_user_id)
    initial, store_path, layout = _derive_publication(installed)
    if initial != rebuilt:
        raise _publication_failure()
    lock_path = store_path / _REPLICA_CONTROL_LOCK_NAME
    _validate_lock_path(store_path, lock_path, required=False)
    marker_path = layout.control_root / _AUTHORITY_MARKER_NAME
    marker_bytes = _expected_marker(initial.target).canonical_bytes()
    pending = _select_marker_authority(
        initial.target.binding,
        marker_json=marker_bytes,
        active_user_id=active_actor,
    )
    if isinstance(pending, FlatProjectAuthority):
        raise _publication_failure()
    carrier_path = _actor_authorization_view(layout.control_root, pending)
    pointer_path = layout.actor / _POINTER_NAME
    verified: GenerationProjectAuthority
    with _native_control_lock(store_path, lock_path, timeout):
        locked, locked_store, locked_layout = _derive_publication(installed)
        if locked != initial or locked_store != store_path or locked_layout != layout:
            raise _publication_failure()
        _reprove_publication_paths(locked, store_path, layout, lock_path)
        marker_json = _read_control_file(store_path, marker_path)
        if marker_json is not None:
            verified, _, _ = _active_selection(
                locked.target, store_path, pending, pointer_path, marker_json
            )
            _audit_installed_root(store_path, layout, locked)
        else:
            carrier_json, carrier = _read_actor_authorization_view(pending)
            if carrier is not None and (
                carrier.installed_for_user_id != active_actor
                or carrier.installed_for_user_id != locked.target.identity.installed_for_user_id
            ):
                raise ReplicaActorMismatchError(_ACTOR_DIFFERENT)
            committed_at = _audit_installed_root(store_path, layout, locked)
            if carrier is not None:
                carrier_exact = _validate_carrier(locked.target, active_actor, marker_json, carrier)
            else:
                carrier_exact = False
            pointer_json = _read_control_file(store_path, pointer_path)
            pointer: InstalledGenerationPointer | None = None
            if carrier_exact and carrier is not None and pointer_json is not None:
                try:
                    candidate = _parse_exact_pointer(pointer_json)
                except GenerationControlCorruptError:
                    candidate = None
                if candidate is not None and _validate_control_pair(
                    locked.target, active_actor, marker_json, carrier, candidate
                ):
                    pointer = candidate

            installed_at: str | None = None
            if not carrier_exact or carrier is None:
                installed_state_id = generate_ulid()
                installed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                carrier = _build_carrier(
                    locked.target, active_actor, committed_at, installed_state_id
                )
                intended_carrier = carrier.canonical_bytes()
                carrier = _write_carrier(
                    locked,
                    store_path,
                    layout,
                    lock_path,
                    marker_path,
                    carrier_path,
                    active_actor,
                    carrier_json,
                    intended_carrier,
                )
                carrier_json = intended_carrier
                pointer = None
            else:
                installed_state_id = carrier.installed_state_id

            assert carrier_json is not None
            if pointer is None:
                if installed_at is None:
                    installed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                pointer = _build_pointer(
                    locked.target,
                    active_actor,
                    committed_at,
                    installed_at,
                    installed_state_id,
                )
                pointer_bytes = pointer.canonical_bytes()
                pointer = _write_pointer(
                    locked,
                    store_path,
                    layout,
                    lock_path,
                    marker_path,
                    carrier_path,
                    pointer_path,
                    active_actor,
                    carrier_json,
                    pointer_json,
                    pointer_bytes,
                )
            else:
                pointer_bytes = pointer.canonical_bytes()

            _reprove_publication_paths(locked, store_path, layout, lock_path)
            observed_carrier = _read_control_file(store_path, carrier_path)
            observed_pointer = _read_control_file(store_path, pointer_path)
            if observed_carrier != carrier_json or observed_pointer != pointer_bytes:
                raise _publication_failure()
            assert observed_carrier is not None
            carrier = _require_published_carrier(
                locked.target, active_actor, observed_carrier, carrier_json
            )
            pointer = _parse_exact_pointer(observed_pointer)
            _require_published_pair(locked.target, active_actor, carrier, pointer)
            _audit_installed_root(store_path, layout, locked)
            if _read_control_file(store_path, marker_path) is not None:
                raise _publication_failure()
            _require_active_actor(active_actor)
            try:
                atomic_write_bytes(marker_path, marker_bytes)
            except Exception:
                pass
            observed_marker = _read_control_file(store_path, marker_path)
            if observed_marker != marker_bytes:
                raise _publication_failure() from None
            verified, final_carrier, final_pointer = _active_selection(
                locked.target,
                store_path,
                pending,
                pointer_path,
                observed_marker,
            )
            if final_carrier != carrier_json or final_pointer != pointer_bytes:
                raise _publication_failure() from None
            _audit_installed_root(store_path, layout, locked)
    _require_active_actor(active_actor)
    return verified


def publish_generation_control(
    installed: InstalledGenerationRoot, *, timeout: float = -1
) -> GenerationProjectAuthority:
    try:
        return _publish_generation_control(installed, timeout)
    except GenerationAuthorityError as exc:
        raise type(exc)(str(exc)) from None


__all__ = (
    "GenerationControlPublicationError GenerationInstallError GenerationRootAudit "
    "GenerationRootDivergedError InstalledGenerationRoot StagedGenerationRoot "
    "audit_generation_tree install_generation_root publish_generation_control "
    "stage_generation_root"
).split()
