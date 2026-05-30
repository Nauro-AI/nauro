"""Atomic text-write primitive for control-plane JSON files.

The single tmp-write-then-``os.replace`` primitive used by the control-plane
JSON writers (``registry.json``, ``config.json``, per-repo ``config.json``).
Durability scope is atomic-replace only: the rename is atomic on a single
filesystem, so a reader never observes a partially written target. There is
deliberately no ``fsync`` — that matches every existing call site, and
crash-durability is an explicit non-goal here.
"""

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically via a tmp sibling and ``os.replace``.

    Creates the parent directory if needed, writes to a ``.tmp`` sibling,
    optionally chmods that tmp file, then atomically renames it over ``path``.
    The chmod is applied to the tmp file before the rename so the target is
    never momentarily readable at a wider mode.

    Args:
        path: Destination file path.
        text: Full file contents to write.
        mode: When set, the permission bits applied to the file (e.g. ``0o600``
            for owner-only). When None, the file keeps the default umask mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)
