"""Crash-safe local installation of verified hosted-generation projections."""

from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

import pydantic as pyd

from nauro.store._atomic import atomic_write_bytes
from nauro.store.generation_authority import (
    GENERATION_CONTROL_SCHEMA_VERSION,
    FlatProjectAuthority,
    GenerationAuthorityError,
    GenerationProjectAuthority,
    GenerationProjectionIdentity,
    InstalledGenerationPointer,
    RefreshRequiredError,
    projection_identity_from_pointer,
    select_project_authority,
)
from nauro.store.generation_projection import VerifiedGenerationProjection
from nauro.store.replica_control import (
    ReplicaControlLayout,
    ReplicaControlReadError,
    _is_link_like,
    _refuse_symlinks,
    locked_replica_control_snapshot,
)
from nauro.store.repo_config import generate_ulid

_LEASE_CREATE_FLAGS = (
    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
)


class GenerationInstallationError(GenerationAuthorityError):
    """A verified generation could not be installed safely."""

    code = "generation_install_failed"


def _installed_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_install_state_id() -> str:
    return generate_ulid()


def _pointer_bytes(pointer: InstalledGenerationPointer) -> bytes:
    return json.dumps(
        pointer.model_dump(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _expected_directories(projection: VerifiedGenerationProjection) -> set[str]:
    directories: set[str] = set()
    for artifact in projection.artifacts:
        parts = artifact.path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return directories


def _audit_root(root: Path, projection: VerifiedGenerationProjection) -> None:
    try:
        unsafe_root = _is_link_like(root)
    except ReplicaControlReadError as exc:
        raise GenerationInstallationError("The generation root metadata is unavailable.") from exc
    if unsafe_root or not root.is_dir():
        raise GenerationInstallationError("The generation root is not a regular directory.")
    expected_files = {artifact.path: artifact.content for artifact in projection.artifacts}
    expected_directories = _expected_directories(projection)
    observed_files: set[str] = set()
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            try:
                unsafe_path = _is_link_like(path)
            except ReplicaControlReadError as exc:
                raise GenerationInstallationError(
                    "The generation root path metadata is unavailable."
                ) from exc
            if unsafe_path:
                raise GenerationInstallationError(
                    "The generation root contains a link or reparse point."
                )
            if path.is_dir():
                if relative not in expected_directories:
                    raise GenerationInstallationError(
                        "The generation root contains an unexpected directory."
                    )
                continue
            if not path.is_file() or relative not in expected_files:
                raise GenerationInstallationError(
                    "The generation root contains an unexpected file."
                )
            if path.read_bytes() != expected_files[relative]:
                raise GenerationInstallationError(
                    f"The installed generation artifact diverges: {relative}."
                )
            observed_files.add(relative)
    except GenerationInstallationError:
        raise
    except OSError as exc:
        raise GenerationInstallationError("The generation root is unreadable.") from exc
    if observed_files != set(expected_files):
        raise GenerationInstallationError("The generation root is incomplete.")


def _discard_staging(path: Path) -> None:
    try:
        if _is_link_like(path):
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except (OSError, ReplicaControlReadError):
        pass


def _stage_projection(
    layout: ReplicaControlLayout,
    projection: VerifiedGenerationProjection,
    install_state_id: str,
) -> Path:
    staging = layout.staging_root(projection.target.identity, install_state_id)
    _refuse_symlinks(layout.store_path, (staging,))
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise GenerationInstallationError("The generation staging root is unavailable.") from exc
    try:
        for artifact in projection.artifacts:
            destination = staging / artifact.path
            _refuse_symlinks(layout.store_path, (destination,))
            atomic_write_bytes(destination, artifact.content)
        _audit_root(staging, projection)
        return staging
    except BaseException:
        _discard_staging(staging)
        raise


def _publish_root(
    layout: ReplicaControlLayout,
    staging: Path,
    projection: VerifiedGenerationProjection,
) -> Path:
    root = layout.generation_root(projection.target.identity)
    _refuse_symlinks(layout.store_path, (root,))
    if root.exists() or root.is_symlink():
        _audit_root(root, projection)
        return root
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(root)
    except OSError as exc:
        raise GenerationInstallationError("The generation root could not be published.") from exc
    _audit_root(root, projection)
    return root


def _ensure_exact_file(path: Path, content: bytes, *, label: str) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise GenerationInstallationError(f"The generation {label} is not a regular file.")
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise GenerationInstallationError(f"The generation {label} is unreadable.") from exc
        if existing != content:
            raise GenerationInstallationError(f"The generation {label} diverges.")
        return
    try:
        atomic_write_bytes(path, content)
        if path.read_bytes() != content:
            raise GenerationInstallationError(f"The generation {label} diverges after write.")
    except GenerationInstallationError:
        raise
    except OSError as exc:
        raise GenerationInstallationError(f"The generation {label} could not be written.") from exc


def _replace_pointer(path: Path, content: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise GenerationInstallationError("The generation pointer is not a regular file.")
    try:
        atomic_write_bytes(path, content)
        if path.read_bytes() != content:
            raise GenerationInstallationError("The generation pointer diverges after write.")
    except GenerationInstallationError:
        raise
    except OSError as exc:
        raise GenerationInstallationError("The generation pointer could not be written.") from exc


def _ensure_lease(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, _LEASE_CREATE_FLAGS, 0o600)
    except FileExistsError:
        try:
            observed = path.lstat()
        except OSError as exc:
            raise GenerationInstallationError("The generation lease is unreadable.") from exc
        if not stat.S_ISREG(observed.st_mode) or observed.st_size != 0:
            raise GenerationInstallationError("The generation lease is not an empty regular file.")
        return
    except OSError as exc:
        raise GenerationInstallationError("The generation lease could not be created.") from exc
    else:
        try:
            os.close(descriptor)
        except OSError as exc:
            raise GenerationInstallationError("The generation lease could not be closed.") from exc


def _current_pointer(
    projection: VerifiedGenerationProjection,
    marker_json: bytes | None,
    pointer_json: bytes | None,
) -> InstalledGenerationPointer | None:
    try:
        authority = select_project_authority(
            projection.target.binding,
            marker_json=marker_json,
            pointer_json=pointer_json,
            active_user_id=projection.target.identity.installed_for_user_id,
            active_projection_scope_id=projection.target.identity.projection_scope_id,
        )
    except RefreshRequiredError:
        if pointer_json is None:
            return None
        try:
            return InstalledGenerationPointer.model_validate_json(pointer_json)
        except pyd.ValidationError as exc:
            raise GenerationInstallationError(
                "The current generation pointer cannot be revalidated."
            ) from exc
    if isinstance(authority, FlatProjectAuthority):
        return None
    if isinstance(authority, GenerationProjectAuthority):
        return authority.pointer
    raise GenerationInstallationError("The current generation authority is unsupported.")


def _is_same_or_refuse_replacement(
    current: InstalledGenerationPointer | None,
    target: GenerationProjectionIdentity,
) -> bool:
    if current is None:
        return False
    current_identity = projection_identity_from_pointer(current)
    if current_identity == target:
        return True
    raise GenerationInstallationError(
        "Replacing an active generation requires a fresh refresh commit."
    )


def _build_pointer(
    projection: VerifiedGenerationProjection,
    install_state_id: str,
) -> InstalledGenerationPointer:
    try:
        return InstalledGenerationPointer(
            **projection.target.identity.model_dump(),
            schema_version=GENERATION_CONTROL_SCHEMA_VERSION,
            installed_at=_installed_at(),
            installed_state_id=install_state_id,
        )
    except pyd.ValidationError as exc:
        raise GenerationInstallationError(
            "The installed generation pointer could not be derived."
        ) from exc


def install_verified_generation(
    projection: VerifiedGenerationProjection,
    *,
    lock_timeout: float = -1,
) -> InstalledGenerationPointer:
    """Install a verified projection and publish its local pointer last."""
    if type(projection) is not VerifiedGenerationProjection:
        raise GenerationInstallationError("Generation installation requires verified bytes.")
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    install_state_id = _new_install_state_id()
    staging = _stage_projection(layout, projection, install_state_id)
    try:
        with locked_replica_control_snapshot(
            projection.target.binding,
            active_user_id=projection.target.identity.installed_for_user_id,
            timeout=lock_timeout,
        ) as snapshot:
            current = _current_pointer(
                projection,
                snapshot.marker_json,
                snapshot.pointer_json,
            )
            same_identity = _is_same_or_refuse_replacement(
                current,
                projection.target.identity,
            )
            root = layout.generation_root(projection.target.identity)
            manifest = layout.generation_manifest(projection.target.identity)
            lease = layout.generation_lease(projection.target.identity)
            pointer_path = layout.actor_pointer(projection.target.identity.installed_for_user_id)
            _refuse_symlinks(
                layout.store_path,
                (root, manifest, lease, pointer_path),
            )
            _publish_root(layout, staging, projection)
            _ensure_exact_file(
                manifest,
                projection.manifest_json,
                label="manifest",
            )
            _ensure_lease(lease)
            if same_identity:
                assert current is not None
                return current
            pointer = _build_pointer(projection, install_state_id)
            pointer_json = _pointer_bytes(pointer)
            _replace_pointer(pointer_path, pointer_json)
            return pointer
    finally:
        _discard_staging(staging)


__all__ = [
    "GenerationInstallationError",
    "install_verified_generation",
]
