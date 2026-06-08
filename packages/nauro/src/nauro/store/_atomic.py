"""Atomic text-write primitive for control-plane JSON files.

The single tmp-write-then-``os.replace`` primitive used by the control-plane
JSON writers (``registry.json``, ``config.json``, per-repo ``config.json``).
Durability scope is atomic-replace only: the rename is atomic on a single
filesystem, so a reader never observes a partially written target. There is
deliberately no ``fsync`` — that matches every existing call site, and
crash-durability is an explicit non-goal here.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically via a tmp sibling and ``os.replace``.

    Creates the parent directory if needed, writes to a tmp sibling, then
    atomically renames it over ``path`` so a reader never observes a partial
    target.

    Args:
        path: Destination file path.
        text: Full file contents to write.
        mode: When set, the permission bits applied to the file (e.g. ``0o600``
            for owner-only). When None, the file keeps the default umask mode.

    When ``mode`` is given the tmp sibling is created owner-only with a random,
    unguessable name via :func:`tempfile.mkstemp` (which opens with
    ``O_CREAT|O_EXCL`` at mode ``0o600``). The contents are therefore never
    momentarily world-readable, and a symlink pre-planted at a predictable
    ``.tmp`` path cannot redirect or clobber the write — this matters for the
    auth token, which is written at ``0o600``. The ``mode=None`` path keeps the
    original default-umask behavior for non-sensitive control-plane JSON.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            if mode != 0o600:
                os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    else:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
