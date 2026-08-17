"""Atomic write primitives for store files.

The single tmp-write-then-``os.replace`` primitive used by the control-plane
JSON writers (``registry.json``, ``config.json``, per-repo ``config.json``), by
``FilesystemStore.write_file`` for every store file, by the graph command for
its rendered HTML, and by the sync pull for the remote bytes it lands.
Durability scope is atomic-replace only: the rename is atomic on a single
filesystem, so a reader never observes a partially written target. There is
deliberately no ``fsync`` — that matches every existing call site, and
crash-durability is an explicit non-goal here.

The text and bytes entry points differ only in how they fill the tmp file. Both
matter for the same reason: a plain write truncates the target first, so a write
that fails halfway leaves a shortened file that still looks like content to
every reader, including the next sync's push.
"""

import os
import secrets
import stat
from pathlib import Path

_TMP_OPEN_FLAGS = (
    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
)

# The tmp sibling's name: a leading dot, the target's name, a random token, and
# this suffix. Minted and recognised through the two functions below rather than
# spelled out anywhere else, because a reader that has to identify an orphan -
# the sync layer, deciding what may be pushed - must not carry its own copy of
# the shape this module writes.
TMP_SUFFIX = ".tmp"
_TMP_TOKEN_BYTES = 8
_HEX = frozenset("0123456789abcdef")


def _tmp_name(name: str) -> str:
    """The name a tmp sibling of ``name`` is born under."""
    return f".{name}.{secrets.token_hex(_TMP_TOKEN_BYTES)}{TMP_SUFFIX}"


def is_tmp_sibling(name: str) -> bool:
    """True for a basename this module could have minted as a tmp sibling.

    Lets a reader recognise the orphan a kill between write and replace leaves.
    """
    if not (name.startswith(".") and name.endswith(TMP_SUFFIX)):
        return False
    token = name[: -len(TMP_SUFFIX)].rsplit(".", 1)[-1]
    return len(token) == _TMP_TOKEN_BYTES * 2 and set(token) <= _HEX


def _open_random_tmp(path: Path, creation_mode: int) -> tuple[int, Path]:
    """Open a randomly named ``O_CREAT|O_EXCL`` tmp sibling of ``path``.

    Exclusive creation never follows a pre-planted symlink, and the mode is a parameter.
    """
    while True:
        tmp = path.parent / _tmp_name(path.name)
        try:
            return os.open(tmp, _TMP_OPEN_FLAGS, creation_mode), tmp
        except FileExistsError:  # pragma: no cover - 64-bit-random collision
            continue


def _resolve_modes(path: Path, mode: int | None) -> tuple[int | None, int]:
    """Return the bits the final file must carry and the bits the tmp is born with.

    A pinned or preserved mode makes the tmp owner-only; a modeless one uses the umask.
    """
    if mode is not None:
        return mode, 0o600
    try:
        return stat.S_IMODE(path.stat().st_mode), 0o600
    except FileNotFoundError:
        # No existing file: the umask-applied creation mode is already the
        # final mode, no chmod needed. Any other stat error (permissions,
        # not-a-directory) propagates rather than silently dropping an
        # existing target's bits.
        return None, 0o666


def _replace(tmp: Path, path: Path, final_mode: int | None) -> None:
    """Put the finished tmp file in place under the mode it must carry."""
    if final_mode is not None and final_mode != 0o600:
        os.chmod(tmp, final_mode)
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically via a tmp sibling and ``os.replace``.

    The bytes counterpart of :func:`atomic_write_text`; an existing target keeps its bits.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    final_mode, creation_mode = _resolve_modes(path, None)
    fd, tmp = _open_random_tmp(path, creation_mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        _replace(tmp, path, final_mode)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path, text: str, *, mode: int | None = None, newline: str | None = None
) -> None:
    """Write ``text`` to ``path`` atomically via a tmp sibling and ``os.replace``.
    ``mode`` pins the permission bits; without it an existing file keeps its own and
    a new one takes the umask. Each writer lands a complete file, last replace wins.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    final_mode, creation_mode = _resolve_modes(path, mode)
    fd, tmp = _open_random_tmp(path, creation_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
        _replace(tmp, path, final_mode)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
