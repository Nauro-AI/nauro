"""Atomic text-write primitive for store files.

The single tmp-write-then-``os.replace`` primitive used by the control-plane
JSON writers (``registry.json``, ``config.json``, per-repo ``config.json``) and
by the graph command for its rendered HTML. Durability scope is atomic-replace
only: the rename is atomic on a single filesystem, so a reader never observes a
partially written target. There is deliberately no ``fsync`` — that matches
every existing call site, and crash-durability is an explicit non-goal here.
"""

import os
import secrets
import stat
from pathlib import Path

_TMP_OPEN_FLAGS = (
    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
)


def _open_random_tmp(path: Path, creation_mode: int) -> tuple[int, Path]:
    """Open a randomly named ``O_CREAT|O_EXCL`` tmp sibling of ``path``.

    Same shape as :func:`tempfile.mkstemp` (unguessable name, exclusive
    creation that never follows a pre-planted symlink, umask applied to the
    creation mode) but with the creation mode as a parameter, so a
    non-sensitive new file can be born at the default umask mode instead of
    ``0o600`` without touching the process-wide umask — ``os.umask`` is
    process-global, and a read-restore dance around it races against every
    other thread creating files.
    """
    while True:
        tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            return os.open(tmp, _TMP_OPEN_FLAGS, creation_mode), tmp
        except FileExistsError:  # pragma: no cover - 64-bit-random collision
            continue


def atomic_write_text(
    path: Path, text: str, *, mode: int | None = None, newline: str | None = None
) -> None:
    """Write ``text`` to ``path`` atomically via a tmp sibling and ``os.replace``.

    Creates the parent directory if needed, writes to a tmp sibling, then
    atomically renames it over ``path`` so a reader never observes a partial
    target.

    Args:
        path: Destination file path.
        text: Full file contents to write.
        mode: When set, the permission bits applied to the file (e.g. ``0o600``
            for owner-only). When None, an existing file keeps its current
            permission bits and a new file gets the default umask mode, the
            same bits a plain ``write_text`` would produce.
        newline: Passed through to the underlying text write. ``"\\n"`` pins LF
            line endings on every platform, so a file written on Windows is
            byte-identical to one written on POSIX (the default would translate
            ``\\n`` to ``\\r\\n`` on Windows). When None, the platform default
            applies, matching the original control-plane behavior.

    Every write goes through a tmp sibling with a random, unguessable name
    opened ``O_CREAT|O_EXCL``, so a symlink pre-planted at a predictable tmp
    path can neither redirect nor clobber the write, and the tmp file is
    removed on any failure. When ``mode`` is given, or when an existing
    target's bits are being preserved, the tmp is created owner-only so the
    contents are never momentarily more readable than the final file — this
    matters for the auth token, which is written at ``0o600``. A brand-new
    modeless file is instead created at the default umask mode directly (the
    kernel applies the umask at creation), matching plain ``write_text`` for
    non-sensitive control-plane JSON without any process-global umask
    manipulation. Guarantees are atomic-replace only: concurrent writers each
    land a complete file (last replace wins, intermediate read-modify-write
    updates can still be lost), and crash durability remains out of scope.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    final_mode: int | None
    if mode is not None:
        final_mode = mode
        creation_mode = 0o600
    else:
        try:
            final_mode = stat.S_IMODE(path.stat().st_mode)
            creation_mode = 0o600
        except OSError:
            # New non-sensitive file: the umask-applied creation mode is
            # already the final mode, no chmod needed.
            final_mode = None
            creation_mode = 0o666
    fd, tmp = _open_random_tmp(path, creation_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
        if final_mode is not None and final_mode != 0o600:
            os.chmod(tmp, final_mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
