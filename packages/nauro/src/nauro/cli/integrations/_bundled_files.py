"""Shared file helpers for the bundled skill and agent codecs."""

from __future__ import annotations

from pathlib import Path

from nauro.store.write_safety import (
    SymlinkRefusal,
    UserSymlinkRefusal,
    find_file_symlink,
    find_symlink,
)


def bundled_file_refusal(
    target: Path, repo: Path | None
) -> SymlinkRefusal | UserSymlinkRefusal | None:
    """Return the symlink refusal for ``target``: repo-relative in ``repo``, else user-global."""
    if repo is None:
        return find_file_symlink(target)
    return find_symlink(repo, target.relative_to(repo).as_posix())


def retained_backup_name(target: Path) -> str | None:
    """Return a sibling ``.bak`` name when that backup is a real file.

    Removal never deletes the backup. It may be the only copy of a local edit.
    """
    backup = target.with_name(target.name + ".bak")
    try:
        if backup.is_symlink() or not backup.is_file():
            return None
    except OSError:
        return None
    return backup.name
