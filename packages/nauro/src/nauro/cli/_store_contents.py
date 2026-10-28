"""Recognize the files of a flat legacy store that Nauro accounts for.

A store-relative path is accounted for when it is a protected-generation member
or belongs to a named Nauro-managed class. Each class reuses the constant or
recognizer of the code that writes it, so there is no blanket exemption for
dotfiles or lock names. ``nauro doctor`` lists everything else as advisory.
"""

from __future__ import annotations

import os
from pathlib import Path

from nauro_core.constants import DECISION_HASHES_FILE, SNAPSHOTS_DIR
from nauro_core.protected_generation_membership import is_protected_generation_member

from nauro.graph import DEFAULT_GRAPH_FILENAME
from nauro.store._atomic import is_tmp_sibling
from nauro.store.journal import JOURNAL_DIR
from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
    _is_link_or_reparse,
)
from nauro.store.store_lock import DIR_LOCK_NAME
from nauro.sync.lock import SYNC_LOCK_FILE
from nauro.sync.merge import CONFLICT_BACKUP_DIR, LOCK_ARTIFACT_SUFFIXES, SPOOL_DIR_PREFIX
from nauro.sync.state import SYNC_STATE_FILE

_MANAGED_ROOT_FILES = frozenset(
    {
        DECISION_HASHES_FILE,
        SYNC_STATE_FILE,
        SYNC_LOCK_FILE,
        DEFAULT_GRAPH_FILENAME,
        _REPLICA_CONTROL_LOCK_NAME,
    }
)
_MANAGED_ROOT_TREES = frozenset(
    {SNAPSHOTS_DIR, JOURNAL_DIR, CONFLICT_BACKUP_DIR, _REPLICA_CONTROL_ROOT_NAME}
)


def _is_managed_root_tree(name: str) -> bool:
    return name in _MANAGED_ROOT_TREES or name.startswith(SPOOL_DIR_PREFIX)


def is_managed_store_path(rel: str) -> bool:
    """True when the POSIX store-relative ``rel`` is a member or a Nauro-managed file."""
    top, separator, _rest = rel.partition("/")
    basename = rel.rsplit("/", 1)[-1]
    return (
        is_protected_generation_member(rel)
        or (bool(separator) and _is_managed_root_tree(top))
        or rel in _MANAGED_ROOT_FILES
        or basename == DIR_LOCK_NAME
        or rel.endswith(LOCK_ARTIFACT_SUFFIXES)
        or is_tmp_sibling(basename)
    )


def _is_plain_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False) and not _is_link_or_reparse(
            entry.stat(follow_symlinks=False)
        )
    except OSError:
        return False


def unknown_store_files(store_path: Path) -> tuple[str, ...]:
    """Every store-relative path that is not accounted for, sorted.

    Links and reparse points are listed, not followed; managed root trees are skipped.
    """
    found: list[str] = []
    pending = [("", store_path)]
    while pending:
        prefix, directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            # An unreadable directory is listed with a trailing slash so doctor still reports.
            found.append(prefix or "./")
            continue
        for entry in entries:
            rel = prefix + entry.name
            if not _is_plain_directory(entry):
                if not is_managed_store_path(rel):
                    found.append(rel)
            elif prefix or not _is_managed_root_tree(entry.name):
                pending.append((rel + "/", Path(entry.path)))
    return tuple(sorted(found))
