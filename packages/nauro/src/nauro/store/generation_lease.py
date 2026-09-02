"""Cross-platform lifetime locks for installed generation roots."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal, cast

from portalocker import AlreadyLocked, LockException, LockFlags, lock, unlock

from nauro.store.generation_authority import (
    GenerationAuthorityError,
    GenerationProjectionIdentity,
)
from nauro.store.replica_control import (
    ReplicaControlLayout,
    ReplicaControlReadError,
    _refuse_symlinks,
)

_LEASE_OPEN_FLAGS = (
    os.O_RDWR
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LeaseKind = Literal["read", "cleanup"]


class GenerationLeaseError(GenerationAuthorityError):
    """An installed generation lease could not preserve lifetime safety."""


class GenerationLeaseBusyError(GenerationLeaseError):
    """A conflicting generation lease is held by another reader or cleanup."""

    code = "generation_lease_busy"


class GenerationLeaseUnavailableError(GenerationLeaseError):
    """The platform or local lease evidence cannot provide the required lock."""

    code = "generation_lease_unavailable"


def _lease_path(
    layout: ReplicaControlLayout,
    identity: GenerationProjectionIdentity,
) -> Path:
    if (
        type(layout) is not ReplicaControlLayout
        or type(identity) is not GenerationProjectionIdentity
    ):
        raise GenerationLeaseUnavailableError("Generation leases require validated control state.")
    path = layout.generation_lease(identity)
    try:
        _refuse_symlinks(layout.store_path, (path,))
    except ReplicaControlReadError as exc:
        raise GenerationLeaseUnavailableError("The generation lease path is unsafe.") from exc
    return path


def _open_lease(path: Path) -> BinaryIO:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise GenerationLeaseUnavailableError("The generation lease is unavailable.") from exc
    if not stat.S_ISREG(observed.st_mode) or observed.st_size != 0:
        raise GenerationLeaseUnavailableError("The generation lease is not an empty regular file.")

    descriptor: int | None = None
    try:
        descriptor = os.open(path, _LEASE_OPEN_FLAGS)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != 0
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise GenerationLeaseUnavailableError("The generation lease changed during open.")
        handle = cast(BinaryIO, os.fdopen(descriptor, "r+b", buffering=0, closefd=True))
        descriptor = None
        return handle
    except GenerationLeaseUnavailableError:
        raise
    except OSError as exc:
        raise GenerationLeaseUnavailableError("The generation lease could not be opened.") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _acquire(handle: BinaryIO, kind: _LeaseKind) -> None:
    flags = LockFlags.SHARED if kind == "read" else LockFlags.EXCLUSIVE
    try:
        lock(handle, flags | LockFlags.NON_BLOCKING)
    except AlreadyLocked as exc:
        raise GenerationLeaseBusyError("A conflicting generation lease is active.") from exc
    except (ImportError, LockException, OSError, RuntimeError) as exc:
        raise GenerationLeaseUnavailableError(
            "The required generation lease is unavailable on this platform."
        ) from exc


def _release(handle: BinaryIO) -> None:
    error: BaseException | None = None
    try:
        unlock(handle)
    except (ImportError, LockException, OSError, RuntimeError) as exc:
        error = exc
    try:
        handle.close()
    except OSError as exc:
        if error is None:
            error = exc
    if error is not None:
        raise GenerationLeaseUnavailableError(
            "The generation lease could not be released."
        ) from error


@contextmanager
def _generation_lease(
    layout: ReplicaControlLayout,
    identity: GenerationProjectionIdentity,
    kind: _LeaseKind,
) -> Iterator[None]:
    handle = _open_lease(_lease_path(layout, identity))
    try:
        _acquire(handle, kind)
    except BaseException:
        try:
            handle.close()
        except OSError:
            pass
        raise
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            _release(handle)
        except GenerationLeaseUnavailableError:
            if not body_failed:
                raise


@contextmanager
def generation_read_lease(
    layout: ReplicaControlLayout,
    identity: GenerationProjectionIdentity,
) -> Iterator[None]:
    """Hold a nonblocking shared lease for one complete generation read."""
    with _generation_lease(layout, identity, "read"):
        yield


@contextmanager
def generation_cleanup_lease(
    layout: ReplicaControlLayout,
    identity: GenerationProjectionIdentity,
) -> Iterator[None]:
    """Hold the nonblocking exclusive lease required before root cleanup."""
    with _generation_lease(layout, identity, "cleanup"):
        yield


__all__ = [
    "GenerationLeaseBusyError",
    "GenerationLeaseError",
    "GenerationLeaseUnavailableError",
    "generation_cleanup_lease",
    "generation_read_lease",
]
