"""Private, process-serialized credentials for explicit reference authentication."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nauro.sync.decision_profile import _private_json


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


class CredentialStore:
    def __init__(self, path: Path, binding: str, actor: str) -> None:
        self.path, self.binding, self.actor = path, binding, actor
        self.marker = path.with_name(path.name + ".renewing")

    @contextlib.contextmanager
    def locked(self, timeout: float = 2.0) -> Iterator[None]:
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
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(16)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
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
        fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b'{"renewal_in_progress":true}')
            stream.flush()
            os.fsync(stream.fileno())
        self.sync_directory()

    def finish(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.sync_directory()

    def sync_directory(self) -> None:
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def empty(self, state: Literal["logged_out", "renewal_in_progress"]) -> CredentialRecord:
        return CredentialRecord(
            revision=secrets.token_hex(32),
            binding=self.binding,
            state=state,
            user_id=self.actor,
        )


def profile_binding(profile: BaseModel) -> str:
    return hashlib.sha256(json.dumps(profile.model_dump(), sort_keys=True).encode()).hexdigest()
