"""Conflict resolution for cloud sync.

When both local and remote changed since last sync, this module decides
how to merge or which version wins.
"""

import logging
import ntpath
import os
import stat
import sys
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any

from nauro.graph import DEFAULT_GRAPH_FILENAME
from nauro.store._atomic import atomic_write_bytes, is_tmp_sibling
from nauro.store.journal import JOURNAL_DIR
from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
)
from nauro.store.store_lock import DIR_LOCK_NAME, RMW_LOCK_SUFFIX
from nauro.sync._path_diagnostics import (
    _MissingPathPolicy,
    _NativeKind,
    _PathAdmission,
    _PathClass,
    _PreparedStoreRoot,
    _SafeWalkEntry,
    _SemanticPathView,
    _StoreRootPreparationError,
    _UnsafeReason,
)
from nauro.sync._windows_long_names import _existing_long_component

logger = logging.getLogger("nauro.sync")

# Append-only logs where a set-union merge is safe. Everything else,
# including decision files (mutable single records rewritten in place by
# update/supersede), resolves by last-write-wins with a recoverable backup,
# because no automatic merge of two divergent rewrites is correct.
_SET_UNION_PATHS = ("open-questions.md", "state_history.md")

# Scratch directory a pull creates for the decision bodies it must hold before
# taking the decision lock. Removed at the end of the run; the prefix keeps a
# directory orphaned by a kill signal out of both sync directions.
SPOOL_DIR_PREFIX = ".pull-spool-"

# Recovery drop-box for content the pull declined to install: the losing side
# of a last-write-wins conflict, and the remote decision behind a quarantined
# number collision.
CONFLICT_BACKUP_DIR = ".conflict-backup"

# Files that are never synced. The graph command's default output lands in the
# store directory; its generation timestamp changes every run, so its sha never
# settles and syncing it would re-push the artifact on every run and fan it out
# to every collaborator. A custom --output path is the user's explicit choice
# and is not guarded here; only the default filename is.
NEVER_SYNC = (".sync-state.json", DEFAULT_GRAPH_FILENAME)

# Lock-file artifacts are local concurrency plumbing, not store content.
# filelock keeps Unix lock files after release as of 3.29.5 (deleting them
# raced concurrent acquirers), so store writes leave these behind: the
# per-target ``<name>.lock`` from write_file (its targets are ``*.md`` files
# plus the ``.decision-hashes.json`` index, hence both suffixes below), the
# read-modify-write ``<name>.rmwlock``, and the bare ``.lock`` directory
# sentinels. Syncing them would fan the droppings out to every collaborator's
# store. The suffixes are deliberately narrow (``.md.lock``, not ``.lock``) so
# a legitimate store file such as ``context/poetry.lock`` still syncs.
LOCK_ARTIFACT_SUFFIXES = (".md.lock", ".json.lock", RMW_LOCK_SUFFIX)

_MAX_NATIVE_LINK_HOPS = 40
_TRAILING_SEPARATOR = object()


@dataclass(frozen=True)
class _ResolvedNative:
    path: Path
    component: str | None
    kind: _NativeKind


def _unsafe(raw_identity: str, reason: _UnsafeReason) -> _PathAdmission:
    return _PathAdmission(_PathClass.UNSAFE, raw_identity, reason=reason)


def _classified(raw_identity: str, path_class: _PathClass) -> _PathAdmission:
    return _PathAdmission(path_class, raw_identity)


def _fold_semantic_component(component: str) -> tuple[str | None, _UnsafeReason | None]:
    base = component.split(":", 1)[0]
    # Windows trims any trailing run of spaces and periods together, so the
    # fold must reach a fixpoint over the set rather than strip each once.
    trimmed = base.lstrip(" ").rstrip(" .")
    if not trimmed:
        spaced = base.strip(" ")
        if spaced == ".":
            return None, _UnsafeReason.FOLDED_DOT
        if spaced == "..":
            return None, _UnsafeReason.FOLDED_PARENT
        return None, _UnsafeReason.FOLDED_EMPTY
    folded = trimmed.casefold()
    if folded == ".":
        return None, _UnsafeReason.FOLDED_DOT
    if folded == "..":
        return None, _UnsafeReason.FOLDED_PARENT
    return folded, None


def _normalize_component_path(
    exact_components: tuple[str, ...] | list[str],
    *,
    raw_identity: str = "",
) -> tuple[_SemanticPathView, _UnsafeReason | None]:
    semantic: list[str] = []
    for native_component in exact_components:
        for component in native_component.replace("\\", "/").split("/"):
            if not component:
                continue
            folded, reason = _fold_semantic_component(component)
            if reason is not None:
                return (
                    _SemanticPathView(raw_identity, tuple(exact_components), tuple(semantic)),
                    reason,
                )
            assert folded is not None
            semantic.append(folded)
    return (
        _SemanticPathView(raw_identity, tuple(exact_components), tuple(semantic)),
        None,
    )


def _classify_component_path(
    exact_components: tuple[str, ...] | list[str],
    *,
    raw_identity: str,
) -> _PathAdmission:
    view, reason = _normalize_component_path(exact_components, raw_identity=raw_identity)
    if reason is not None:
        return _unsafe(raw_identity, reason)
    if not view.semantic_components:
        return _unsafe(raw_identity, _UnsafeReason.EMPTY_PATH)
    if view.semantic_components[0] in {
        _REPLICA_CONTROL_ROOT_NAME.casefold(),
        _REPLICA_CONTROL_LOCK_NAME.casefold(),
    }:
        return _classified(raw_identity, _PathClass.RESERVED_CONTROL)
    return _classified(raw_identity, _PathClass.ORDINARY)


def _sync_input_failure(raw_path: str) -> _UnsafeReason | None:
    folded = raw_path.replace("/", "\\").casefold()
    if folded.startswith(("\\\\?\\", "\\??\\", "\\\\.\\", "\\device\\")):
        return _UnsafeReason.RAW_DEVICE
    if raw_path.startswith(("\\\\", "//")):
        return _UnsafeReason.RAW_UNC
    if ntpath.splitdrive(raw_path)[0]:
        return _UnsafeReason.RAW_DRIVE
    if raw_path.startswith("/"):
        return _UnsafeReason.RAW_ABSOLUTE
    if raw_path.startswith("\\"):
        return _UnsafeReason.RAW_ROOTED
    components = raw_path.replace("\\", "/").split("/")
    if ".." in components:
        return _UnsafeReason.RAW_PARENT
    return None


def _classify_sync_path(raw_relative_path: str) -> _PathAdmission:
    reason = _sync_input_failure(raw_relative_path)
    if reason is not None:
        return _unsafe(raw_relative_path, reason)
    components = [
        component
        for component in raw_relative_path.replace("\\", "/").split("/")
        if component not in {"", "."}
    ]
    return _classify_component_path(components, raw_identity=raw_relative_path)


def _prepare_store_root(configured_root: Path) -> _PreparedStoreRoot:
    try:
        canonical_root = configured_root.resolve(strict=True)
        if not canonical_root.is_absolute() or not canonical_root.is_dir():
            raise OSError
        anchor = canonical_root.anchor
        anchor_parts = Path(anchor).parts
        canonical_parts = canonical_root.parts[len(anchor_parts) :]
        return _PreparedStoreRoot(
            configured_root=configured_root,
            canonical_root=canonical_root,
            native_anchor=anchor,
            canonical_parts=canonical_parts,
        )
    except (OSError, RuntimeError, ValueError):
        raise _StoreRootPreparationError() from None


def _native_relative_parts(raw_path: str) -> tuple[list[str | object], bool]:
    parts: list[str | object]
    if sys.platform == "win32":
        trailing = raw_path.endswith(("\\", "/"))
        parts = [part for part in raw_path.replace("/", "\\").split("\\") if part]
    else:
        trailing = raw_path.endswith("/")
        parts = [part for part in raw_path.split("/") if part]
    if trailing:
        parts.append(_TRAILING_SEPARATOR)
    return parts, trailing


def _normalized_windows_target(target: str) -> tuple[str | None, _UnsafeReason | None]:
    folded = target.casefold()
    for prefix in ("\\\\?\\unc\\", "\\??\\unc\\"):
        if folded.startswith(prefix):
            return "\\\\" + target[len(prefix) :], None
    for prefix in ("\\\\?\\", "\\??\\"):
        if folded.startswith(prefix):
            remainder = target[len(prefix) :]
            if remainder.casefold().startswith("volume{"):
                return None, _UnsafeReason.UNSUPPORTED_REPARSE
            if ntpath.splitdrive(remainder)[0]:
                return remainder, None
            return None, _UnsafeReason.UNSUPPORTED_REPARSE
    if folded.startswith(("\\\\.\\", "\\device\\")):
        return None, _UnsafeReason.UNSUPPORTED_REPARSE
    return target, None


def _absolute_target_suffix(
    target: str, store_root: _PreparedStoreRoot
) -> tuple[list[str] | None, _UnsafeReason | None, bool]:
    if sys.platform == "win32":
        target, reason = _normalized_windows_target(target)
        if reason is not None or target is None:
            return None, reason, False
        drive, remainder = ntpath.splitdrive(target)
        rooted = remainder.startswith(("\\", "/"))
        if not drive and rooted:
            return None, _UnsafeReason.OUTSIDE_STORE, False
        if drive and not rooted:
            return None, _UnsafeReason.OUTSIDE_STORE, False
        if not drive:
            parts = [part for part in target.replace("/", "\\").split("\\") if part]
            return parts, None, False
        root_drive = ntpath.splitdrive(store_root.native_anchor)[0]
        if ntpath.normcase(drive) != ntpath.normcase(root_drive):
            return None, _UnsafeReason.OUTSIDE_STORE, True
        parts = [part for part in remainder.replace("/", "\\").split("\\") if part]
    else:
        if not target.startswith("/"):
            return [part for part in target.split("/") if part], None, False
        parts = [part for part in target.split("/") if part]

    prefix = store_root.canonical_parts
    if len(parts) < len(prefix) or tuple(parts[: len(prefix)]) != prefix:
        return None, _UnsafeReason.OUTSIDE_STORE, True
    return parts[len(prefix) :], None, True


def _metadata_kind(metadata: Any) -> _NativeKind:
    if stat.S_ISDIR(metadata.st_mode):
        return _NativeKind.DIRECTORY
    if stat.S_ISREG(metadata.st_mode):
        return _NativeKind.REGULAR_FILE
    return _NativeKind.IRREGULAR


def _is_link_or_reparse(metadata: Any) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _is_supported_link(metadata: Any) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    tag = getattr(metadata, "st_reparse_tag", None)
    return tag in {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
    }


def _missing_admission(
    *,
    raw_identity: str,
    missing: _MissingPathPolicy,
    stack: list[_ResolvedNative],
    absent_component: str,
    pending: deque[str | object],
) -> _PathAdmission:
    if missing is _MissingPathPolicy.OBSERVED:
        return _unsafe(raw_identity, _UnsafeReason.OBSERVATION_LOST)
    if missing is _MissingPathPolicy.OPTIONAL_FIXED_LEAF:
        if pending:
            return _unsafe(raw_identity, _UnsafeReason.OBSERVATION_LOST)
        return _PathAdmission(
            _PathClass.ORDINARY,
            raw_identity,
            exists=False,
            missing_policy=missing,
        )

    semantic = [entry.component for entry in stack[1:] if entry.component is not None]
    semantic.append(absent_component)
    while pending:
        component = pending.popleft()
        if component is _TRAILING_SEPARATOR:
            continue
        assert isinstance(component, str)
        if component == ".":
            continue
        if component == "..":
            return _unsafe(raw_identity, _UnsafeReason.FOLDED_PARENT)
        semantic.append(component)
        classified = _classify_component_path(semantic, raw_identity=raw_identity)
        if classified.path_class is not _PathClass.ORDINARY:
            return classified
    return _PathAdmission(
        _PathClass.ORDINARY,
        raw_identity,
        exists=False,
        missing_policy=missing,
    )


def _admit_native_path(
    store_root: _PreparedStoreRoot,
    raw_relative_path: str,
    *,
    missing: _MissingPathPolicy,
) -> _PathAdmission:
    initial = _classify_sync_path(raw_relative_path)
    if initial.path_class is not _PathClass.ORDINARY:
        return initial
    if "\x00" in raw_relative_path:
        return _unsafe(raw_relative_path, _UnsafeReason.METADATA_UNAVAILABLE)
    pending_list, _ = _native_relative_parts(raw_relative_path)
    pending: deque[str | object] = deque(pending_list)
    stack = [_ResolvedNative(store_root.canonical_root, None, _NativeKind.DIRECTORY)]
    followed_links: set[tuple[int, int, str]] = set()
    link_hops = 0

    while pending:
        if stack[-1].kind is not _NativeKind.DIRECTORY:
            return _unsafe(raw_relative_path, _UnsafeReason.NON_DIRECTORY_PARENT)
        component = pending.popleft()
        if component is _TRAILING_SEPARATOR or component == ".":
            continue
        assert isinstance(component, str)
        if component == "..":
            if len(stack) == 1:
                return _unsafe(raw_relative_path, _UnsafeReason.OUTSIDE_STORE)
            stack.pop()
            continue

        exact_components = [
            entry.component for entry in stack[1:] if entry.component is not None
        ] + [component]
        lexical = _classify_component_path(exact_components, raw_identity=raw_relative_path)
        if lexical.path_class is not _PathClass.ORDINARY:
            return lexical
        parent = stack[-1].path
        try:
            long_component = _existing_long_component(parent, component)
        except (OSError, ValueError):
            return _unsafe(raw_relative_path, _UnsafeReason.WINDOWS_NAME_LOOKUP_FAILED)
        if long_component is None:
            return _missing_admission(
                raw_identity=raw_relative_path,
                missing=missing,
                stack=stack,
                absent_component=component,
                pending=pending,
            )
        long_components = [
            entry.component for entry in stack[1:] if entry.component is not None
        ] + [long_component]
        admitted_name = _classify_component_path(long_components, raw_identity=raw_relative_path)
        if admitted_name.path_class is not _PathClass.ORDINARY:
            return admitted_name

        candidate = parent / component
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            return _missing_admission(
                raw_identity=raw_relative_path,
                missing=missing,
                stack=stack,
                absent_component=component,
                pending=pending,
            )
        except (OSError, ValueError):
            return _unsafe(raw_relative_path, _UnsafeReason.METADATA_UNAVAILABLE)

        if not _is_link_or_reparse(metadata):
            stack.append(_ResolvedNative(candidate, long_component, _metadata_kind(metadata)))
            continue
        if not _is_supported_link(metadata):
            return _unsafe(raw_relative_path, _UnsafeReason.UNSUPPORTED_REPARSE)
        identity = (metadata.st_dev, metadata.st_ino, os.fspath(candidate))
        if identity in followed_links:
            return _unsafe(raw_relative_path, _UnsafeReason.LINK_LOOP)
        followed_links.add(identity)
        link_hops += 1
        if link_hops > _MAX_NATIVE_LINK_HOPS:
            return _unsafe(raw_relative_path, _UnsafeReason.LINK_HOP_LIMIT)
        try:
            target = os.readlink(candidate)
        except (OSError, ValueError):
            return _unsafe(raw_relative_path, _UnsafeReason.LINK_TARGET_UNREADABLE)
        target_parts, reason, absolute = _absolute_target_suffix(target, store_root)
        if reason is not None or target_parts is None:
            return _unsafe(raw_relative_path, reason or _UnsafeReason.OUTSIDE_STORE)
        if absolute:
            stack = [_ResolvedNative(store_root.canonical_root, None, _NativeKind.DIRECTORY)]
        target_pending: list[str | object] = list(target_parts)
        if target.endswith(("/", "\\")):
            target_pending.append(_TRAILING_SEPARATOR)
        pending.extendleft(reversed(target_pending))

    final = stack[-1]
    return _PathAdmission(
        _PathClass.ORDINARY,
        raw_relative_path,
        exists=True,
        missing_policy=missing,
        native_kind=final.kind,
    )


def _walk_unsafe(
    native_path: Path, raw_relative_path: str, reason: _UnsafeReason
) -> _SafeWalkEntry:
    return _SafeWalkEntry(native_path, raw_relative_path, _unsafe(raw_relative_path, reason))


def _list_directory(directory: Path) -> list[os.DirEntry[str]] | None:
    try:
        with os.scandir(directory) as listing:
            return list(listing)
    except (OSError, ValueError):
        return None


def _walk_store_files(store_root: _PreparedStoreRoot) -> Iterator[_SafeWalkEntry]:
    # Explicit frames rather than generator recursion, so a deep tree cannot
    # raise RecursionError out of the walk.
    frames: list[tuple[Path, tuple[str, ...], deque[os.DirEntry[str]]]] = []

    def enter(directory: Path, relative_parts: tuple[str, ...]) -> _SafeWalkEntry | None:
        entries = _list_directory(directory)
        if entries is None:
            raw_directory = os.path.join(*relative_parts) if relative_parts else ""
            return _walk_unsafe(directory, raw_directory, _UnsafeReason.METADATA_UNAVAILABLE)
        frames.append((directory, relative_parts, deque(entries)))
        return None

    blocked = enter(store_root.canonical_root, ())
    if blocked is not None:
        yield blocked
        return
    while frames:
        directory, relative_parts, entries = frames[-1]
        if not entries:
            frames.pop()
            continue
        entry = entries.popleft()
        parts = (*relative_parts, entry.name)
        raw_relative = os.path.join(*parts)
        lexical = _classify_component_path(parts, raw_identity=raw_relative)
        if lexical.path_class is _PathClass.RESERVED_CONTROL:
            continue
        if lexical.path_class is _PathClass.UNSAFE:
            yield _SafeWalkEntry(Path(entry.path), raw_relative, lexical)
            continue
        try:
            long_component = _existing_long_component(directory, entry.name)
        except (OSError, ValueError):
            yield _walk_unsafe(
                Path(entry.path), raw_relative, _UnsafeReason.WINDOWS_NAME_LOOKUP_FAILED
            )
            continue
        if long_component is None:
            yield _walk_unsafe(Path(entry.path), raw_relative, _UnsafeReason.OBSERVATION_LOST)
            continue
        long_parts = (*relative_parts, long_component)
        named = _classify_component_path(long_parts, raw_identity=raw_relative)
        if named.path_class is _PathClass.RESERVED_CONTROL:
            continue
        if named.path_class is _PathClass.UNSAFE:
            yield _SafeWalkEntry(Path(entry.path), raw_relative, named)
            continue
        try:
            direct = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            yield _walk_unsafe(Path(entry.path), raw_relative, _UnsafeReason.OBSERVATION_LOST)
            continue
        except (OSError, ValueError):
            yield _walk_unsafe(Path(entry.path), raw_relative, _UnsafeReason.METADATA_UNAVAILABLE)
            continue
        if _is_link_or_reparse(direct):
            admission = _admit_native_path(
                store_root, raw_relative, missing=_MissingPathPolicy.OBSERVED
            )
            if admission.path_class is _PathClass.RESERVED_CONTROL:
                continue
            if admission.path_class is _PathClass.UNSAFE:
                yield _SafeWalkEntry(Path(entry.path), raw_relative, admission)
                continue
            try:
                followed = entry.stat(follow_symlinks=True)
            except FileNotFoundError:
                yield _walk_unsafe(Path(entry.path), raw_relative, _UnsafeReason.OBSERVATION_LOST)
                continue
            except (OSError, ValueError):
                yield _walk_unsafe(
                    Path(entry.path), raw_relative, _UnsafeReason.METADATA_UNAVAILABLE
                )
                continue
            if _metadata_kind(followed) is _NativeKind.REGULAR_FILE:
                yield _SafeWalkEntry(Path(entry.path), raw_relative, admission)
            continue
        kind = _metadata_kind(direct)
        admission = _PathAdmission(
            _PathClass.ORDINARY,
            raw_relative,
            exists=True,
            missing_policy=_MissingPathPolicy.OBSERVED,
            native_kind=kind,
        )
        if kind is _NativeKind.REGULAR_FILE:
            yield _SafeWalkEntry(Path(entry.path), raw_relative, admission)
        elif kind is _NativeKind.DIRECTORY:
            blocked = enter(Path(entry.path), parts)
            if blocked is not None:
                yield blocked


def normalize_rel(relative_path: str) -> str:
    """Return a store-relative path with POSIX separators.

    Every prefix and suffix check in the sync layer works on this form.
    """
    return relative_path.replace("\\", "/")


def should_skip(relative_path: str) -> bool:
    """Return True if this file should never be synced."""
    normalized = normalize_rel(relative_path)
    if normalized in NEVER_SYNC:
        return True
    if normalized.split("/", 1)[0].startswith(SPOOL_DIR_PREFIX):
        return True
    # The write-path provenance journal is store-local by design: it is
    # excluded from cloud sync in v1 (both its events log and its lock).
    if normalized.startswith(JOURNAL_DIR + "/"):
        return True
    basename = normalized.rsplit("/", 1)[-1]
    # A half-written sibling from the atomic write primitive. The write removes
    # its own tmp file on failure, but a kill signal between the write and the
    # replace strands a complete-looking copy of a store file under a name
    # nothing reads - and syncing it would publish a version the user never had
    # and install it for every collaborator. The shape is recognised by
    # ``_atomic`` itself, so the writer and this exclusion cannot drift.
    if is_tmp_sibling(basename):
        return True
    return basename == DIR_LOCK_NAME or normalized.endswith(LOCK_ARTIFACT_SUFFIXES)


def write_backup(project_path: Path, backup_name: str, content: bytes) -> Path:
    """Write ``content`` into ``.conflict-backup/`` under ``backup_name``.

    Written atomically: this is the only copy of content the pull declined to install.
    """
    backup_dir = project_path / CONFLICT_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / backup_name
    atomic_write_bytes(backup_path, content)
    logger.info("Conflict backup saved: %s", backup_path)
    return backup_path


def _save_conflict_backup(project_path: Path, relative_path: str, content: bytes) -> Path:
    """Save the losing version to .conflict-backup/."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = relative_path.replace("%", "%25").replace("/", "%2F").replace("\\", "%5C")
    return write_backup(project_path, f"{timestamp}-{filename}", content)


class Side(Enum):
    """Which copy of a file survives on disk when no merge is defined.

    The caller decides, because the answer is about the path's history rather
    than its content: a version this store published has an ancestor the two
    sides share, and one it never published does not.
    """

    local = auto()
    remote = auto()


def resolve_conflict(
    project_path: Path,
    local_path: Path,
    remote_content: bytes,
    relative_path: str,
    *,
    keeps: Side,
) -> bytes:
    """Resolve a conflict between local and remote versions.
    A ``_SET_UNION_PATHS`` file set-unions whatever ``keeps`` asks, since a union has
    no losing side; any other keeps the named side and backs the other one up.
    """
    local_content = local_path.read_bytes()

    if relative_path in _SET_UNION_PATHS:
        return _set_union_markdown(local_content, remote_content)

    loser, winner = (
        (remote_content, local_content) if keeps is Side.local else (local_content, remote_content)
    )
    _save_conflict_backup(project_path, relative_path, loser)
    logger.warning(
        "Conflict on %s resolved by last-write-wins (kept %s). "
        "The other version was saved to .conflict-backup/",
        relative_path,
        keeps.name,
    )
    return winner


def _parse_sections(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split lines into a preamble and a list of (header, body) sections.
    A section starts at any line beginning with "## " (level-2 ATX); the preamble is everything
    before the first header, and each body runs to the next header or EOF.
    """
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_body: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if current_header is None:
                # Close out the preamble; start the first section.
                pass
            else:
                sections.append((current_header, current_body))
            current_header = line
            current_body = []
            continue
        if current_header is None:
            preamble.append(line)
        else:
            current_body.append(line)

    if current_header is not None:
        sections.append((current_header, current_body))

    return preamble, sections


def _dedupe_preserve_order(lines: list[str]) -> list[str]:
    """Drop exact-duplicate non-blank lines, preserving first-occurrence order.

    Blank lines pass through undeduped, so the merged output keeps its structure.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line == "":
            out.append(line)
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _set_union_markdown(local: bytes, remote: bytes) -> bytes:
    """Section-aware set-union merge of two append-only markdown files; pure function, no I/O.
    Emits the union of the preambles, then per-header body unions in local order with
    remote-only sections appended last; non-blank lines are deduped at document scope.
    """
    local_text = local.decode("utf-8")
    remote_text = remote.decode("utf-8")

    local_lines = local_text.split("\n")
    remote_lines = remote_text.split("\n")

    # split("\n") on a trailing-newline string yields a final "" element. That's
    # actual content for the dedupe step (blank lines are preserved), so we drop
    # the synthetic trailing "" and re-add a single newline at the end.
    local_trailing_nl = local_text.endswith("\n")
    remote_trailing_nl = remote_text.endswith("\n")
    if local_trailing_nl and local_lines and local_lines[-1] == "":
        local_lines = local_lines[:-1]
    if remote_trailing_nl and remote_lines and remote_lines[-1] == "":
        remote_lines = remote_lines[:-1]

    local_preamble, local_sections = _parse_sections(local_lines)
    remote_preamble, remote_sections = _parse_sections(remote_lines)

    # Group sections by header so a header that appears multiple times in one
    # source (the corrupted-file case where the whole document was duplicated)
    # collapses into a single emitted section with the union of all bodies.
    section_order: list[str] = []
    bodies_by_header: dict[str, list[str]] = {}
    for header, body in list(local_sections) + list(remote_sections):
        if header not in bodies_by_header:
            section_order.append(header)
            bodies_by_header[header] = []
        bodies_by_header[header].extend(body)

    merged: list[str] = []
    merged.extend(_dedupe_preserve_order(local_preamble + remote_preamble))

    for header in section_order:
        merged.append(header)
        merged.extend(_dedupe_preserve_order(bodies_by_header[header]))

    # Final pass: dedupe non-blank lines across the whole document. Within each
    # section the body has already been deduped against itself, but a corrupted
    # file may carry a stray "# Title" line (or repeated entries) inside a
    # section body that also lives in the preamble. Drop those exact-duplicate
    # non-blank lines while preserving blanks and first-occurrence order.
    deduped = _dedupe_preserve_order(merged)

    result = "\n".join(deduped)
    if local_trailing_nl or remote_trailing_nl:
        result += "\n"
    return result.encode("utf-8")
