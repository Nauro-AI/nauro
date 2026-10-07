"""Private, process-serialized credentials for explicit reference authentication."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nauro.store import _platform_durability as durability
from nauro.store import _windows_security as security
from nauro.store.replica_control import (
    ReplicaControlBusyError,
    ReplicaControlReadError,
    _is_link_or_reparse,
    _native_control_lock,
)
from nauro.sync.decision_profile import _private_json

_NOFOLLOW, _BINARY = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_BINARY", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class CredentialRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[2] = 2
    revision: str
    binding: str
    state: Literal["active", "logged_out", "renewal_in_progress"]
    user_id: str
    access_token: str = Field(default="", repr=False)
    refresh_token: str = Field(default="", repr=False)
    expires_at: int = 0

    def needs_verification(self) -> bool:
        return (
            self.state == "renewal_in_progress"
            and bool(self.access_token)
            and bool(self.refresh_token)
            and self.expires_at == 0
        )


class CredentialStore:
    def __init__(self, path: Path, binding: str, actor: str) -> None:
        self.path, self.binding, self.actor = path, binding, actor
        self.marker = path.with_name(path.name + ".renewing")

    @contextlib.contextmanager
    def locked(self, timeout: float = 2.0) -> Iterator[None]:
        if durability.WINDOWS:
            with self._windows_locked(timeout):
                yield
            return
        if sys.platform == "win32":
            raise ValueError("POSIX credential locking is unavailable on Windows")
        import fcntl

        parent = self.path.parent
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Credentials require an owner-only directory")
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.path.with_name(self.path.name + ".lock"), flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Unsafe credential lock")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("Reference credentials busy") from None
                    time.sleep(0.02)
            # Complete a prior marker removal before admitting stored credentials.
            self.sync_directory()
            yield
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _windows_locked(self, timeout: float) -> Iterator[None]:
        parent, lock = self.path.parent, self.path.with_name(self.path.name + ".lock")
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("Credentials require an owner-only directory")
        for part in (current := Path(os.path.abspath(parent)), *current.parents):
            if _is_link_or_reparse(os.lstat(part)):
                raise security.OwnerOnlyError(f"Credentials cannot live under a link: {part}")
        security.require_owner_only(parent, directory=True)
        with contextlib.suppress(FileExistsError):
            os.close(os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | _BINARY, 0o600))
            security.set_owner_only(lock)
        inside = False
        try:
            with _native_control_lock(parent, lock, timeout):
                security.require_owner_only(lock)
                self.sync_directory()
                inside = True
                yield
                inside = False
        except (ReplicaControlBusyError, ReplicaControlReadError) as exc:
            if inside:
                raise
            busy = isinstance(exc, ReplicaControlBusyError)
            message = "Reference credentials busy" if busy else "Unsafe credential lock"
            raise ValueError(message) from exc

    def read(self) -> CredentialRecord | None:
        try:
            if self.path.lstat().st_nlink != 1:
                raise ValueError("Credential hard links are refused")
            record = CredentialRecord.model_validate(_private_json(self.path))
        except FileNotFoundError:
            return None
        if record.binding != self.binding or record.user_id != self.actor:
            raise ValueError("Credentials differ from the selected profile")
        return record

    def write(self, record: CredentialRecord) -> None:
        if self.path.exists() or self.path.is_symlink():
            self.read()
        data = record.model_dump_json().encode()
        if len(data) > 65536:
            raise ValueError("Credentials exceed size limit")
        self.write_private(self.path, data)

    def write_private(self, path: Path, data: bytes) -> None:
        if path.parent != self.path.parent or len(data) > 65536:
            raise ValueError("Invalid private record location or size")
        if path.exists() or path.is_symlink():
            _private_json(path)
            if path.lstat().st_nlink != 1:
                raise ValueError("Private record hard links are refused")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                self._protect(temporary)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            durability.durable_rename(temporary, path)
            self.sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def incomplete(self) -> bool:
        if self.marker.exists() or self.marker.is_symlink():
            _private_json(self.marker)
            return True
        return False

    def begin(self) -> None:
        if self.incomplete():
            return
        fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
        self._protect(self.marker, fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b'{"renewal_in_progress":true}')
            stream.flush()
            os.fsync(stream.fileno())
        self.sync_directory()

    def finish(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.sync_directory()

    def sync_directory(self) -> None:
        if durability.WINDOWS:
            return
        directory = os.open(self.path.parent, os.O_RDONLY | _DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _protect(self, path: Path, fd: int | None = None) -> None:
        if not durability.WINDOWS:
            return
        try:
            security.set_owner_only(path)
        except BaseException:
            if fd is not None:  # a marker must not outlive a failed protection
                os.close(fd)
                path.unlink(missing_ok=True)
            raise

    def empty(self, state: Literal["logged_out", "renewal_in_progress"]) -> CredentialRecord:
        return CredentialRecord(
            revision=secrets.token_hex(32),
            binding=self.binding,
            state=state,
            user_id=self.actor,
        )


def profile_binding(profile: BaseModel) -> str:
    return hashlib.sha256(json.dumps(profile.model_dump(), sort_keys=True).encode()).hexdigest()
