"""Read-only Store access to one pointer-selected hosted generation."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import InitVar, dataclass, field
from pathlib import Path

from nauro_core.constants import DECISIONS_DIR
from nauro_core.protected_generation_membership import (
    InvalidGenerationPath,
    validate_protected_generation_path,
)

from nauro.store.generation_authority import (
    GenerationAuthorityError,
    GenerationProjectAuthority,
    projection_identity_from_pointer,
    select_project_authority,
)
from nauro.store.generation_lease import generation_read_lease
from nauro.store.generation_projection import (
    GenerationProjectionManifest,
    GenerationProjectionTarget,
    verify_generation_projection_manifest,
)
from nauro.store.replica_control import (
    ReplicaControlLayout,
    ReplicaControlReadError,
    _is_link_like,
    _refuse_symlinks,
    locked_replica_control_snapshot,
)
from nauro.store.resolution import ResolvedProjectBinding

_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_STORE_CONSTRUCTION_TOKEN = object()


class GenerationStoreError(GenerationAuthorityError):
    """A pointer-selected generation cannot satisfy the Store contract."""


class GenerationStoreUnavailableError(GenerationStoreError):
    """Generation bytes or control state cannot support a safe read."""

    code = "generation_store_unavailable"


class GenerationStoreReadOnlyError(GenerationStoreError):
    """A protected hosted generation cannot be changed through the file Store."""

    code = "generation_store_read_only"


def _read_digest_bound_file(
    containment_root: Path,
    path: Path,
    expected_digest: str,
    *,
    label: str,
) -> bytes:
    try:
        unsafe_root = _is_link_like(containment_root)
    except ReplicaControlReadError as exc:
        raise GenerationStoreUnavailableError(
            f"The generation {label} containment root is unsafe."
        ) from exc
    if unsafe_root or not containment_root.is_dir():
        raise GenerationStoreUnavailableError(f"The generation {label} containment root is unsafe.")
    try:
        _refuse_symlinks(containment_root, (path,))
        observed = path.lstat()
    except ReplicaControlReadError as exc:
        raise GenerationStoreUnavailableError(f"The generation {label} path is unsafe.") from exc
    except OSError as exc:
        raise GenerationStoreUnavailableError(f"The generation {label} is unavailable.") from exc
    if not stat.S_ISREG(observed.st_mode):
        raise GenerationStoreUnavailableError(f"The generation {label} is not a regular file.")

    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or opened.st_size != observed.st_size
        ):
            raise GenerationStoreUnavailableError(f"The generation {label} changed during open.")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            content = handle.read(opened.st_size + 1)
            finished = os.fstat(handle.fileno())
        if (
            len(content) != opened.st_size
            or (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
            or finished.st_size != opened.st_size
        ):
            raise GenerationStoreUnavailableError(f"The generation {label} changed during read.")
    except GenerationStoreUnavailableError:
        raise
    except (OSError, OverflowError, ValueError) as exc:
        raise GenerationStoreUnavailableError(f"The generation {label} could not be read.") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise GenerationStoreUnavailableError(f"The generation {label} digest diverges.")
    return content


def _expected_directories(manifest: GenerationProjectionManifest) -> set[str]:
    directories: set[str] = set()
    for artifact_path in manifest.artifacts:
        parts = artifact_path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return directories


def _audit_root_membership(root: Path, manifest: GenerationProjectionManifest) -> None:
    expected_files = set(manifest.artifacts)
    expected_directories = _expected_directories(manifest)
    observed_files: set[str] = set()
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            try:
                unsafe_path = _is_link_like(path)
            except ReplicaControlReadError as exc:
                raise GenerationStoreUnavailableError(
                    "The selected generation path metadata is unavailable."
                ) from exc
            if unsafe_path:
                raise GenerationStoreUnavailableError(
                    "The selected generation contains a link or reparse point."
                )
            if path.is_dir():
                if relative not in expected_directories:
                    raise GenerationStoreUnavailableError(
                        "The selected generation contains an unexpected directory."
                    )
                continue
            if not path.is_file() or relative not in expected_files:
                raise GenerationStoreUnavailableError(
                    "The selected generation contains an unexpected file."
                )
            observed_files.add(relative)
    except GenerationStoreUnavailableError:
        raise
    except (OSError, RuntimeError) as exc:
        raise GenerationStoreUnavailableError(
            "The selected generation cannot be enumerated safely."
        ) from exc
    if observed_files != expected_files:
        raise GenerationStoreUnavailableError("The selected generation is incomplete.")


@dataclass(frozen=True, eq=False)
class LeasedGenerationStore:
    """A read-only Store valid only inside its lifetime-lease context."""

    _root: Path = field(repr=False)
    _manifest: GenerationProjectionManifest = field(repr=False)
    _construction_token: InitVar[object]
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _STORE_CONSTRUCTION_TOKEN:
            raise GenerationStoreUnavailableError(
                "Generation stores must be opened through their lifetime-lease context."
            )

    def _require_open(self) -> None:
        if self._closed:
            raise GenerationStoreUnavailableError("The generation read lease has ended.")

    def _read_canonical(self, path: str) -> str | None:
        self._require_open()
        expected_digest = self._manifest.artifacts.get(path)
        if expected_digest is None:
            return None
        content = _read_digest_bound_file(
            self._root,
            self._root / path,
            expected_digest,
            label=f"artifact {path}",
        )
        return content.decode("utf-8", errors="replace")

    def read_file(self, path: str) -> str | None:
        try:
            canonical = validate_protected_generation_path(path)
        except InvalidGenerationPath as exc:
            raise GenerationStoreUnavailableError(
                "The requested path is outside the protected generation."
            ) from exc
        return self._read_canonical(canonical)

    def write_file(self, path: str, content: str) -> None:
        self._require_open()
        raise GenerationStoreReadOnlyError(
            "Hosted generation files require a named typed operation."
        )

    def delete_file(self, path: str) -> None:
        self._require_open()
        raise GenerationStoreReadOnlyError(
            "Hosted generation files require a named typed operation."
        )

    def list_decisions(self) -> list[str]:
        self._require_open()
        prefix = f"{DECISIONS_DIR}/"
        suffix = ".md"
        return sorted(
            path[len(prefix) : -len(suffix)]
            for path in self._manifest.artifacts
            if path.startswith(prefix) and path.endswith(suffix)
        )

    def read_decision(self, file_stem: str) -> str | None:
        return self.read_file(f"{DECISIONS_DIR}/{file_stem}.md")

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        self._require_open()
        return {stem: self.read_decision(stem) for stem in stems}

    def _close(self) -> None:
        object.__setattr__(self, "_closed", True)


def _require_generation_authority(
    binding: ResolvedProjectBinding,
    *,
    marker_json: bytes | None,
    pointer_json: bytes | None,
    active_user_id: str,
    active_projection_scope_id: str,
) -> GenerationProjectAuthority:
    authority = select_project_authority(
        binding,
        marker_json=marker_json,
        pointer_json=pointer_json,
        active_user_id=active_user_id,
        active_projection_scope_id=active_projection_scope_id,
    )
    if not isinstance(authority, GenerationProjectAuthority):
        raise GenerationStoreUnavailableError(
            "The project has not selected hosted generation authority."
        )
    return authority


@contextmanager
def leased_generation_store(
    binding: ResolvedProjectBinding,
    *,
    active_user_id: str,
    active_projection_scope_id: str,
) -> Iterator[LeasedGenerationStore]:
    """Yield one pointer-bound Store while its shared lifetime lease is held."""
    if type(binding) is not ResolvedProjectBinding:
        raise GenerationStoreUnavailableError("Generation reads require a validated binding.")
    layout = ReplicaControlLayout(binding.store_path)
    with ExitStack() as lifetime_leases:
        with locked_replica_control_snapshot(
            binding,
            active_user_id=active_user_id,
        ) as snapshot:
            initial = _require_generation_authority(
                binding,
                marker_json=snapshot.marker_json,
                pointer_json=snapshot.pointer_json,
                active_user_id=active_user_id,
                active_projection_scope_id=active_projection_scope_id,
            )
            identity = projection_identity_from_pointer(initial.pointer)
            root = layout.generation_root(identity)
            manifest_path = layout.generation_manifest(identity)
            try:
                _refuse_symlinks(binding.store_path, (root, manifest_path))
            except ReplicaControlReadError as exc:
                raise GenerationStoreUnavailableError(
                    "The selected generation paths are unsafe."
                ) from exc
            lifetime_leases.enter_context(generation_read_lease(layout, identity))
            current = _require_generation_authority(
                binding,
                marker_json=snapshot.marker_json,
                pointer_json=snapshot.reread_pointer(),
                active_user_id=active_user_id,
                active_projection_scope_id=active_projection_scope_id,
            )
            if current.pointer != initial.pointer:
                raise GenerationStoreUnavailableError(
                    "The generation pointer changed while acquiring its read lease."
                )

        try:
            _refuse_symlinks(binding.store_path, (root, manifest_path))
            unsafe_root = _is_link_like(root)
        except ReplicaControlReadError as exc:
            raise GenerationStoreUnavailableError(
                "The selected generation paths are unsafe."
            ) from exc
        if unsafe_root or not root.is_dir():
            raise GenerationStoreUnavailableError(
                "The selected generation root is not a regular directory."
            )
        manifest_json = _read_digest_bound_file(
            binding.store_path,
            manifest_path,
            identity.manifest_digest,
            label="manifest",
        )
        target = GenerationProjectionTarget(binding=binding, identity=identity)
        manifest = verify_generation_projection_manifest(target, manifest_json)
        _audit_root_membership(root, manifest)
        store = LeasedGenerationStore(root, manifest, _STORE_CONSTRUCTION_TOKEN)
        try:
            yield store
        finally:
            store._close()


__all__ = [
    "GenerationStoreError",
    "GenerationStoreReadOnlyError",
    "GenerationStoreUnavailableError",
    "LeasedGenerationStore",
    "leased_generation_store",
]
