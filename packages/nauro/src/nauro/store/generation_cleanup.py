"""Lease-fenced cleanup and crash recovery for unpointed generation roots."""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from nauro_core.identifiers import IdentifierKind, validate_identifier

from nauro.store.generation_authority import (
    GenerationAuthorityError,
    InstalledGenerationPointer,
    select_generation_refresh_base,
)
from nauro.store.generation_lease import (
    GenerationLeaseBusyError,
    _generation_cleanup_lease_path,
)
from nauro.store.replica_control import (
    _GENERATIONS_DIRECTORY,
    _LEASES_DIRECTORY,
    _PROJECTIONS_DIRECTORY,
    _TOMBSTONES_DIRECTORY,
    ReplicaControlLayout,
    ReplicaControlReadError,
    _is_link_like,
    _refuse_symlinks,
    locked_replica_control_snapshot,
)
from nauro.store.repo_config import generate_ulid
from nauro.store.resolution import ResolvedProjectBinding


class GenerationCleanupError(GenerationAuthorityError):
    """Local generation cleanup could not prove that deletion was safe."""

    code = "generation_cleanup_failed"


@dataclass(frozen=True)
class GenerationCleanupReport:
    """Completed and deferred cleanup work for one actor."""

    recovered_tombstones: tuple[str, ...]
    deleted_generations: tuple[str, ...]
    busy: tuple[str, ...]


@dataclass(frozen=True, order=True)
class _GenerationKey:
    user_id: str
    projection_scope_id: str
    generation_id: str
    root: Path = field(compare=False, repr=False)
    lease: Path = field(compare=False, repr=False)
    tombstone_parent: Path = field(compare=False, repr=False)

    @property
    def label(self) -> str:
        return f"{self.user_id}/{self.projection_scope_id}/{self.generation_id}"

    @property
    def selector(self) -> tuple[str, str, str]:
        return self.user_id, self.projection_scope_id, self.generation_id


@dataclass(frozen=True, order=True)
class _TombstoneKey:
    generation: _GenerationKey
    cleanup_state_id: str
    path: Path = field(compare=False, repr=False)

    @property
    def label(self) -> str:
        return f"{self.generation.label}/{self.cleanup_state_id}"


def _scope_id(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise GenerationCleanupError("The generation inventory contains an invalid scope.")
    return value


def _ulid(value: str, *, field: str) -> str:
    try:
        return validate_identifier(IdentifierKind.ulid, value, field=field)
    except ValueError as exc:
        raise GenerationCleanupError(
            f"The generation inventory contains an invalid {field}."
        ) from exc


def _directories(path: Path, *, label: str) -> tuple[Path, ...]:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise GenerationCleanupError(f"The generation {label} is unavailable.") from exc
    try:
        unsafe = _is_link_like(path)
    except ReplicaControlReadError as exc:
        raise GenerationCleanupError(f"The generation {label} is unsafe.") from exc
    if unsafe or not stat.S_ISDIR(observed.st_mode):
        raise GenerationCleanupError(f"The generation {label} is not a regular directory.")
    try:
        entries = tuple(sorted(path.iterdir(), key=lambda entry: entry.name))
        for entry in entries:
            entry_observed = entry.lstat()
            if _is_link_like(entry) or not stat.S_ISDIR(entry_observed.st_mode):
                raise GenerationCleanupError(f"The generation {label} contains an unsafe entry.")
        return entries
    except GenerationCleanupError:
        raise
    except (OSError, ReplicaControlReadError) as exc:
        raise GenerationCleanupError(f"The generation {label} cannot be enumerated.") from exc


def _inventory(
    layout: ReplicaControlLayout,
    user_id: str,
) -> tuple[tuple[_GenerationKey, ...], tuple[_TombstoneKey, ...]]:
    roots: list[_GenerationKey] = []
    tombstones: list[_TombstoneKey] = []
    projections = layout.actor_pointer(user_id).parent / _PROJECTIONS_DIRECTORY
    for scope_path in _directories(projections, label="projection inventory"):
        scope_id = _scope_id(scope_path.name)
        roots_root = scope_path / _GENERATIONS_DIRECTORY
        leases_root = scope_path / _LEASES_DIRECTORY
        tombstones_root = scope_path / _TOMBSTONES_DIRECTORY
        for root in _directories(
            roots_root,
            label="root inventory",
        ):
            generation_id = _ulid(root.name, field="generation_id")
            roots.append(
                _GenerationKey(
                    user_id,
                    scope_id,
                    generation_id,
                    root,
                    leases_root / f"{generation_id}.lock",
                    tombstones_root / generation_id,
                )
            )
        for generation_path in _directories(tombstones_root, label="tombstone inventory"):
            generation_id = _ulid(generation_path.name, field="generation_id")
            generation = _GenerationKey(
                user_id,
                scope_id,
                generation_id,
                roots_root / generation_id,
                leases_root / f"{generation_id}.lock",
                generation_path,
            )
            for tombstone in _directories(
                generation_path,
                label="tombstone generation inventory",
            ):
                tombstones.append(
                    _TombstoneKey(
                        generation,
                        _ulid(tombstone.name, field="cleanup_state_id"),
                        tombstone,
                    )
                )
    return tuple(sorted(roots)), tuple(sorted(tombstones))


def _pointer_selector(pointer: InstalledGenerationPointer) -> tuple[str, str, str]:
    return (
        pointer.installed_for_user_id,
        pointer.projection_scope_id,
        pointer.generation_id,
    )


def _audit_removable_tree(layout: ReplicaControlLayout, path: Path) -> None:
    try:
        _refuse_symlinks(layout.store_path, (path,))
        observed = path.lstat()
        unsafe = _is_link_like(path)
    except (OSError, ReplicaControlReadError) as exc:
        raise GenerationCleanupError("The generation cleanup root is unavailable.") from exc
    if unsafe or not stat.S_ISDIR(observed.st_mode):
        raise GenerationCleanupError("The generation cleanup root is unsafe.")
    try:
        for entry in path.rglob("*"):
            entry_observed = entry.lstat()
            if _is_link_like(entry) or not (
                stat.S_ISDIR(entry_observed.st_mode) or stat.S_ISREG(entry_observed.st_mode)
            ):
                raise GenerationCleanupError("The generation cleanup root is unsafe.")
    except GenerationCleanupError:
        raise
    except (OSError, ReplicaControlReadError, RuntimeError) as exc:
        raise GenerationCleanupError("The generation cleanup root cannot be audited.") from exc


def _delete_tombstone(layout: ReplicaControlLayout, path: Path) -> None:
    _audit_removable_tree(layout, path)
    try:
        shutil.rmtree(path)
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GenerationCleanupError("The generation tombstone could not be deleted.") from exc
    raise GenerationCleanupError("The generation tombstone remains after deletion.")


def _rename_to_tombstone(
    layout: ReplicaControlLayout,
    key: _GenerationKey,
) -> Path:
    cleanup_state_id = _ulid(generate_ulid(), field="cleanup_state_id")
    tombstone = key.tombstone_parent / cleanup_state_id
    try:
        _refuse_symlinks(layout.store_path, (key.root, tombstone.parent, tombstone))
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        _refuse_symlinks(layout.store_path, (key.root, tombstone.parent, tombstone))
        if tombstone.exists() or tombstone.is_symlink():
            raise GenerationCleanupError("The generation tombstone already exists.")
        key.root.rename(tombstone)
    except GenerationCleanupError:
        raise
    except (OSError, ReplicaControlReadError) as exc:
        raise GenerationCleanupError("The generation root could not be tombstoned.") from exc
    return tombstone


def cleanup_actor_generations(
    binding: ResolvedProjectBinding,
    *,
    active_user_id: str,
    lock_timeout: float = -1,
) -> GenerationCleanupReport:
    """Recover tombstones and delete lease-free roots not selected by the actor pointer."""
    if type(binding) is not ResolvedProjectBinding or binding.mode != "cloud":
        raise GenerationCleanupError("Generation cleanup requires a validated cloud binding.")
    layout = ReplicaControlLayout(binding.store_path)
    recovered: list[str] = []
    deleted: list[str] = []
    busy: list[str] = []
    with locked_replica_control_snapshot(
        binding,
        active_user_id=active_user_id,
        timeout=lock_timeout,
    ) as snapshot:
        pointer = select_generation_refresh_base(
            binding,
            marker_json=snapshot.marker_json,
            pointer_json=snapshot.pointer_json,
            active_user_id=active_user_id,
        )
        roots, tombstones = _inventory(layout, pointer.installed_for_user_id)
        for tombstone in tombstones:
            _audit_removable_tree(layout, tombstone.path)
        for root in roots:
            _audit_removable_tree(layout, root.root)

        for tombstone in tombstones:
            try:
                with _generation_cleanup_lease_path(
                    layout,
                    tombstone.generation.lease,
                ):
                    _delete_tombstone(layout, tombstone.path)
            except GenerationLeaseBusyError:
                busy.append(f"tombstone:{tombstone.label}")
            else:
                recovered.append(tombstone.label)

        for root in roots:
            current = select_generation_refresh_base(
                binding,
                marker_json=snapshot.marker_json,
                pointer_json=snapshot.reread_pointer(),
                active_user_id=active_user_id,
            )
            if root.selector == _pointer_selector(current):
                continue
            try:
                with _generation_cleanup_lease_path(layout, root.lease):
                    tombstone_path = _rename_to_tombstone(layout, root)
                    _delete_tombstone(layout, tombstone_path)
            except GenerationLeaseBusyError:
                busy.append(f"generation:{root.label}")
            else:
                deleted.append(root.label)

    return GenerationCleanupReport(
        recovered_tombstones=tuple(recovered),
        deleted_generations=tuple(deleted),
        busy=tuple(busy),
    )


__all__ = [
    "GenerationCleanupError",
    "GenerationCleanupReport",
    "cleanup_actor_generations",
]
