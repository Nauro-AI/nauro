"""Lock-held authority reads for local hosted-replica control state."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from filelock import FileLock, SoftFileLock, Timeout, UnixFileLock, WindowsFileLock
from nauro_core.constants import HOSTED_STORE_FORMAT_VERSION

from nauro.store.generation_authority import (
    FlatProjectAuthority,
    GenerationAuthorityError,
    InstalledAuthorizationView,
    ProjectAuthority,
    _parse_authorization_view,
    _PendingGenerationAuthority,
    _select_generation_pointer,
    _select_marker_authority,
)
from nauro.store.registry import get_store_path_v2
from nauro.store.resolution import ResolvedProjectBinding

_MAX_CONTROL_FILE_BYTES = 16 * 1024
_EXPIRED_MESSAGE = "Replica control snapshot is no longer lock-bound."
_REPLICA_CONTROL_ROOT_NAME = ".replica"
_REPLICA_CONTROL_LOCK_NAME = ".replica-control.lock"
_READ_FLAGS = sum(
    getattr(os, name, 0)
    for name in ("O_RDONLY", "O_CLOEXEC", "O_BINARY", "O_NOFOLLOW", "O_NONBLOCK")
)
_ANCHOR_FLAGS = sum(
    getattr(os, name, 0)
    for name in ("O_RDWR", "O_CREAT", "O_CLOEXEC", "O_BINARY", "O_NOFOLLOW", "O_NONBLOCK")
)


class ReplicaControlReadError(GenerationAuthorityError):
    """Replica control state cannot be read safely."""

    code = "generation_control_unavailable"


class ReplicaControlBusyError(GenerationAuthorityError):
    """Another process holds the replica control lock."""

    code = "generation_control_busy"


@dataclass(frozen=True)
class ReplicaControlSnapshot:
    """Resolved authority and its exact lock-held control bytes."""

    authority: ProjectAuthority
    marker_json: bytes | None
    pointer_json: bytes | None
    _reread: Callable[[], ProjectAuthority] = field(repr=False, compare=False)

    def reread_authority(self) -> ProjectAuthority:
        """Resolve the bound actor pointer again while the original lock is held."""
        return self._reread()


def _actor_pointer(control_root: Path, pending: _PendingGenerationAuthority) -> Path:
    version = f"v{HOSTED_STORE_FORMAT_VERSION}"
    return control_root / version / "actors" / pending.active_user_id / "pointer.json"


def _actor_authorization_view(control_root: Path, pending: _PendingGenerationAuthority) -> Path:
    version = f"v{pending.marker.store_format_version}"
    return control_root / version / "actors" / pending.active_user_id / "authorization-view.json"


def _is_link_or_reparse(metadata: Any) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _validate_store_path(binding: ResolvedProjectBinding) -> Path:
    path = binding.store_path
    try:
        managed = path == get_store_path_v2(binding.project_id)
        canonical = path.name == binding.project_id and (
            managed or path.is_absolute() and str(path.resolve(strict=False)) == str(path)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReplicaControlReadError("The project store path is unavailable.") from exc
    if not canonical:
        raise ReplicaControlReadError("The project store path is not canonical.")

    current = Path() if managed else Path(path.anchor)
    for part in (path,) if managed else path.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ReplicaControlReadError("The project store path is unavailable.") from exc
        if _is_link_or_reparse(metadata):
            raise ReplicaControlReadError("Replica control paths cannot contain links.")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReplicaControlReadError("The project store is not a directory.")
    return path


def _validate_managed_path(store_path: Path, path: Path) -> None:
    try:
        parts = path.relative_to(store_path).parts
    except ValueError as exc:
        raise ReplicaControlReadError("Replica control path escaped the project store.") from exc
    current = store_path
    for part in ("", *parts):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReplicaControlReadError("Replica control path metadata is unavailable.") from exc
        if _is_link_or_reparse(metadata):
            raise ReplicaControlReadError("Replica control paths cannot contain links.")


def _read_optional_file(path: Path) -> bytes | None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReplicaControlReadError("Replica control file metadata is unavailable.") from exc
    if (
        _is_link_or_reparse(observed)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_size > _MAX_CONTROL_FILE_BYTES
    ):
        raise ReplicaControlReadError("Replica control file is not a bounded regular file.")

    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        opened = os.fstat(descriptor)
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or opened.st_size != observed.st_size
            or opened.st_size > _MAX_CONTROL_FILE_BYTES
        ):
            raise ReplicaControlReadError("Replica control file changed during open.")
        content = os.read(descriptor, _MAX_CONTROL_FILE_BYTES + 1)
        finished = os.fstat(descriptor)
        if (
            _is_link_or_reparse(finished)
            or not stat.S_ISREG(finished.st_mode)
            or finished.st_nlink != 1
            or len(content) > _MAX_CONTROL_FILE_BYTES
            or (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
            or finished.st_size != opened.st_size
            or len(content) != finished.st_size
        ):
            raise ReplicaControlReadError("Replica control file changed during read.")
        try:
            final = os.lstat(path)
        except OSError as exc:
            raise ReplicaControlReadError("Replica control file changed during read.") from exc
        if (
            _is_link_or_reparse(final)
            or not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino, final.st_size)
            != (observed.st_dev, observed.st_ino, observed.st_size)
        ):
            raise ReplicaControlReadError("Replica control file changed during read.")
        return content
    except OSError as exc:
        raise ReplicaControlReadError("Replica control file is unreadable.") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _read_actor_authorization_view(
    pending: _PendingGenerationAuthority,
) -> tuple[bytes | None, InstalledAuthorizationView | None]:
    store_path = _validate_store_path(pending.binding)
    control_root = store_path / _REPLICA_CONTROL_ROOT_NAME
    path = _actor_authorization_view(control_root, pending)
    _validate_managed_path(store_path, path)
    raw = _read_optional_file(path)
    if raw is None:
        return None, None
    return raw, _parse_authorization_view(raw)


def _new_native_lock(path: Path, timeout: float):
    lock = FileLock(str(path), timeout=timeout)
    if isinstance(lock, SoftFileLock) or type(lock) not in (UnixFileLock, WindowsFileLock):
        raise ReplicaControlReadError("Native replica control locking is unavailable.")
    return lock


def _open_lock_anchor(path: Path) -> int | None:
    if sys.platform != "win32":
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _ANCHOR_FLAGS, 0o666)
        opened, observed = os.fstat(descriptor), os.lstat(path)
        if (
            _is_link_or_reparse(opened)
            or _is_link_or_reparse(observed)
            or not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise ReplicaControlReadError("Replica control lock path is unsafe.")
        return descriptor
    except (ReplicaControlReadError, OSError) as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if isinstance(exc, ReplicaControlReadError):
            raise
        raise ReplicaControlReadError("Replica control lock is unavailable.") from exc


@contextmanager
def _native_control_lock(store_path: Path, path: Path, timeout: float) -> Iterator[None]:
    lock = _new_native_lock(path, timeout)
    anchor = _open_lock_anchor(path)
    acquired = False
    primary: BaseException | None = None
    try:
        try:
            lock.acquire()
        except Timeout as exc:
            raise ReplicaControlBusyError("Replica control state is busy.") from exc
        except (OSError, RuntimeError) as exc:
            raise ReplicaControlReadError("Replica control lock is unavailable.") from exc
        acquired = True
        if isinstance(lock, SoftFileLock) or type(lock) not in (UnixFileLock, WindowsFileLock):
            raise ReplicaControlReadError("Native replica control locking is unavailable.")
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failure: BaseException | None = None
        if acquired:
            try:
                lock.release()
                _validate_managed_path(store_path, path)
                metadata = os.lstat(path)
                if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                    raise ReplicaControlReadError("Replica control lock path is unsafe.")
            except BaseException as exc:
                failure = exc
        if anchor is not None:
            try:
                os.close(anchor)
            except BaseException as exc:
                failure = failure or exc
        if failure is not None and primary is None:
            raise ReplicaControlReadError("Replica control lock release failed.") from failure


@contextmanager
def locked_replica_control_snapshot(
    binding: ResolvedProjectBinding,
    *,
    active_user_id: str | None,
    active_projection_scope_id: str | None,
    timeout: float = -1,
) -> Iterator[ReplicaControlSnapshot]:
    """Yield resolved control state while holding its stable native project lock."""
    if binding.mode == "local":
        lock = nullcontext()
    else:
        store_path = _validate_store_path(binding)
        control_lock = store_path / _REPLICA_CONTROL_LOCK_NAME
        control_root = store_path / _REPLICA_CONTROL_ROOT_NAME
        authority_marker = control_root / "authority.json"
        _validate_managed_path(store_path, control_lock)
        lock = _native_control_lock(store_path, control_lock, timeout)
    with lock:
        pointer_path = None
        if binding.mode == "local":
            authority, marker_json, pointer_json = FlatProjectAuthority(binding), None, None
        else:
            _validate_managed_path(store_path, authority_marker)
            marker_json = _read_optional_file(authority_marker)
            selected = _select_marker_authority(
                binding, marker_json=marker_json, active_user_id=active_user_id
            )
            if isinstance(selected, FlatProjectAuthority):
                authority, pointer_json = selected, None
            else:
                pointer_path = _actor_pointer(control_root, selected)
                _validate_managed_path(store_path, pointer_path)
                pointer_json = _read_optional_file(pointer_path)
                authority = _select_generation_pointer(
                    selected,
                    pointer_json=pointer_json,
                    active_projection_scope_id=active_projection_scope_id,
                )

        active, guard = True, Lock()

        def reread() -> ProjectAuthority:
            with guard:
                if not active:
                    raise ReplicaControlReadError(_EXPIRED_MESSAGE)
                if pointer_path is None:
                    return authority
                _validate_managed_path(store_path, pointer_path)
                return _select_generation_pointer(
                    selected,
                    pointer_json=_read_optional_file(pointer_path),
                    active_projection_scope_id=active_projection_scope_id,
                )

        try:
            yield ReplicaControlSnapshot(authority, marker_json, pointer_json, reread)
        finally:
            interruption: BaseException | None = None
            while active:
                try:
                    with guard:
                        active = False
                except BaseException as exc:
                    interruption = interruption or exc
            if interruption is not None:
                raise interruption


__all__ = [
    "ReplicaControlBusyError",
    "ReplicaControlReadError",
    "ReplicaControlSnapshot",
    "locked_replica_control_snapshot",
]
