from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from nauro_core.identifiers import IdentifierKind, validate_identifier

from nauro.store._atomic import _open_random_tmp
from nauro.store.generation_installation import _read_expected
from nauro.store.generation_refresh_intent import MAX_INTENT_BYTES
from nauro.store.generation_refresh_state import GenerationRefreshEvidenceError
from nauro.store.replica_control import (
    _READ_FLAGS,
    _is_link_or_reparse,
    _validate_managed_path,
    _validate_store_path,
)
from nauro.store.resolution import ResolvedProjectBinding


@dataclass(frozen=True)
class RefreshPaths:
    store: Path
    actor: Path

    @property
    def marker(self) -> Path:
        return self.store / ".replica/authority.json"

    @property
    def pointer(self) -> Path:
        return self.actor / "pointer.json"

    @property
    def carrier(self) -> Path:
        return self.actor / "authorization-view.json"

    @property
    def intent(self) -> Path:
        return self.actor / "refresh-intent.json"

    @property
    def history(self) -> Path:
        return self.actor / "refresh-history"


def refresh_paths(binding: ResolvedProjectBinding, actor: str) -> RefreshPaths:
    validate_identifier(IdentifierKind.ulid, actor, field="actor")
    store = _validate_store_path(binding)
    paths = RefreshPaths(store, store / ".replica/v1/actors" / actor)
    _validate_managed_path(store, paths.actor)
    return paths


def read_evidence(paths: RefreshPaths, path: Path, *, archive: bool = False) -> bytes | None:
    _validate_managed_path(paths.store, path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if (
        _is_link_or_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or (before.st_nlink != 1 and not archive)
        or before.st_size > MAX_INTENT_BYTES
    ):
        raise GenerationRefreshEvidenceError("Refresh evidence is not a bounded regular file.")
    identity = before.st_dev, before.st_ino
    raw = _read_expected(path, before.st_size, identity, "refresh evidence")
    _validate_managed_path(paths.store, path)
    after = path.lstat()
    if (
        _is_link_or_reparse(after)
        or (after.st_dev, after.st_ino, after.st_size, after.st_nlink)
        != (before.st_dev, before.st_ino, before.st_size, before.st_nlink)
        or len(raw) != before.st_size
    ):
        raise GenerationRefreshEvidenceError("Refresh evidence changed during read.")
    return raw


def sync_directory(paths: RefreshPaths, path: Path) -> None:
    _validate_managed_path(paths.store, path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        before = path.lstat()
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise GenerationRefreshEvidenceError("Refresh directory identity changed.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_file(paths: RefreshPaths, path: Path) -> None:
    _validate_managed_path(paths.store, path)
    descriptor = os.open(path, _READ_FLAGS)
    try:
        opened, before = os.fstat(descriptor), path.lstat()
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise GenerationRefreshEvidenceError("Refresh file identity changed.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_parents(paths: RefreshPaths, path: Path) -> None:
    path.relative_to(paths.store)
    while True:
        sync_directory(paths, path)
        if path == paths.store:
            return
        path = path.parent


def durable_replace(paths: RefreshPaths, path: Path, raw: bytes) -> None:
    _validate_managed_path(paths.store, path)
    descriptor, temporary = _open_random_tmp(path, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_managed_path(paths.store, path)
        os.replace(temporary, path)
        sync_directory(paths, path.parent)
        if read_evidence(paths, path) != raw:
            raise GenerationRefreshEvidenceError("Refresh replacement readback differs.")
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def preserve_predecessor(paths: RefreshPaths, digest: str, raw: bytes) -> None:
    import hashlib

    if hashlib.sha256(raw).hexdigest() != digest:
        raise GenerationRefreshEvidenceError("Refresh archive digest differs.")
    _validate_managed_path(paths.store, paths.history)
    paths.history.mkdir(exist_ok=True)
    _validate_managed_path(paths.store, paths.history)
    archive = paths.history / f"{digest}.json"
    existing = read_evidence(paths, archive, archive=True)
    if existing is None:
        descriptor, temporary = _open_random_tmp(archive, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            # A crash after link can retain the temporary alias; archive reads verify exact bytes.
            os.link(temporary, archive)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    if read_evidence(paths, archive, archive=True) != raw:
        raise GenerationRefreshEvidenceError("Refresh archive is occupied by different evidence.")
    sync_file(paths, archive)
    sync_parents(paths, paths.history)
