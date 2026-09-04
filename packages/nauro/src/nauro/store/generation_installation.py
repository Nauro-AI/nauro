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
from pathlib import Path

from nauro.store._atomic import atomic_write_bytes, is_tmp_sibling
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
)
from nauro.store.replica_control import (
    _READ_FLAGS,
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
    _is_link_or_reparse,
    _native_control_lock,
    _validate_managed_path,
    _validate_store_path,
)
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


class GenerationInstallError(GenerationAuthorityError):
    code = "generation_install_failed"


class GenerationRootDivergedError(GenerationAuthorityError):
    code = "generation_root_diverged"


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


__all__ = (
    "GenerationInstallError GenerationRootAudit GenerationRootDivergedError "
    "InstalledGenerationRoot StagedGenerationRoot audit_generation_tree "
    "install_generation_root stage_generation_root"
).split()
