"""Lock-held reads of local hosted-replica control state."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout
from nauro_core.constants import HOSTED_STORE_FORMAT_VERSION
from nauro_core.identifiers import IdentifierKind, is_identifier, validate_identifier

from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.resolution import ResolvedProjectBinding

_CONTROL_DIRECTORY = ".replica"
_CONTROL_LOCK_FILE = ".replica-control.lock"
_AUTHORITY_MARKER_FILE = "authority.json"
_ACTORS_DIRECTORY = "actors"
_POINTER_FILE = "pointer.json"
_MAX_CONTROL_FILE_BYTES = 16 * 1024
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class ReplicaControlReadError(GenerationAuthorityError):
    """Replica control state cannot be read safely."""

    code = "generation_control_unavailable"


class ReplicaControlBusyError(GenerationAuthorityError):
    """Another process holds the replica control lock."""

    code = "generation_control_busy"


@dataclass(frozen=True)
class ReplicaControlLayout:
    """Stable paths for one project's versioned replica control state."""

    store_path: Path

    @property
    def control_lock(self) -> Path:
        return self.store_path / _CONTROL_LOCK_FILE

    @property
    def control_root(self) -> Path:
        return self.store_path / _CONTROL_DIRECTORY

    @property
    def authority_marker(self) -> Path:
        return self.control_root / _AUTHORITY_MARKER_FILE

    @property
    def version_root(self) -> Path:
        return self.control_root / f"v{HOSTED_STORE_FORMAT_VERSION}"

    def actor_pointer(self, user_id: str) -> Path:
        canonical = validate_identifier(IdentifierKind.ulid, user_id, field="user_id")
        return self.version_root / _ACTORS_DIRECTORY / canonical / _POINTER_FILE


@dataclass(frozen=True)
class ReplicaControlSnapshot:
    """Exact marker and active-actor pointer bytes read under one lock."""

    marker_json: bytes | None
    pointer_json: bytes | None


def _refuse_symlinks(store_path: Path, paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            relative = path.relative_to(store_path)
        except ValueError as exc:
            raise ReplicaControlReadError(
                "Replica control path escaped the project store."
            ) from exc
        current = store_path
        for part in relative.parts:
            current /= part
            try:
                is_link = current.is_symlink()
            except OSError as exc:
                raise ReplicaControlReadError(
                    "Replica control path metadata is unavailable."
                ) from exc
            if is_link:
                raise ReplicaControlReadError("Replica control paths cannot contain symlinks.")


def _read_optional_file(path: Path) -> bytes | None:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReplicaControlReadError("Replica control file metadata is unavailable.") from exc
    if not stat.S_ISREG(observed.st_mode) or observed.st_size > _MAX_CONTROL_FILE_BYTES:
        raise ReplicaControlReadError("Replica control file is not a bounded regular file.")

    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or opened.st_size > _MAX_CONTROL_FILE_BYTES
        ):
            raise ReplicaControlReadError("Replica control file changed during open.")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            content = handle.read(_MAX_CONTROL_FILE_BYTES + 1)
            finished = os.fstat(handle.fileno())
        if (
            len(content) > _MAX_CONTROL_FILE_BYTES
            or (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
            or finished.st_size != opened.st_size
            or len(content) != finished.st_size
        ):
            raise ReplicaControlReadError("Replica control file changed during read.")
        return content
    except ReplicaControlReadError:
        raise
    except OSError as exc:
        raise ReplicaControlReadError("Replica control file is unreadable.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def locked_replica_control_snapshot(
    binding: ResolvedProjectBinding,
    *,
    active_user_id: str | None,
    timeout: float = -1,
) -> Iterator[ReplicaControlSnapshot]:
    """Yield exact control bytes while holding the project control lock."""
    if binding.mode == "local":
        yield ReplicaControlSnapshot(marker_json=None, pointer_json=None)
        return

    layout = ReplicaControlLayout(binding.store_path)
    _refuse_symlinks(binding.store_path, (layout.control_lock,))
    lock = FileLock(str(layout.control_lock), timeout=timeout)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ReplicaControlBusyError("Replica control state is busy.") from exc
    except OSError as exc:
        raise ReplicaControlReadError("Replica control lock is unavailable.") from exc
    try:
        _refuse_symlinks(binding.store_path, (layout.authority_marker,))
        marker_json = _read_optional_file(layout.authority_marker)
        pointer_json = None
        if marker_json is not None and is_identifier(IdentifierKind.ulid, active_user_id):
            assert isinstance(active_user_id, str)
            pointer = layout.actor_pointer(active_user_id)
            _refuse_symlinks(binding.store_path, (pointer,))
            pointer_json = _read_optional_file(pointer)
        yield ReplicaControlSnapshot(
            marker_json=marker_json,
            pointer_json=pointer_json,
        )
    finally:
        lock.release()


__all__ = [
    "ReplicaControlBusyError",
    "ReplicaControlLayout",
    "ReplicaControlReadError",
    "ReplicaControlSnapshot",
    "locked_replica_control_snapshot",
]
