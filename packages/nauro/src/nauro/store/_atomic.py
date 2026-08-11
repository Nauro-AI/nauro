"""Atomic write primitives for store files.

The single tmp-write-then-``os.replace`` primitive used by the control-plane
JSON writers (``registry.json``, ``config.json``, per-repo ``config.json``), by
the graph command for its rendered HTML, and by the sync pull for the remote
bytes it lands. Durability scope is atomic-replace only: the rename is atomic on
a single filesystem, so a reader never observes a partially written target.
There is deliberately no ``fsync`` — that matches every existing call site, and
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

    A write is removed on any failure, but a kill signal lands between the write
    and the replace often enough to matter, and what it leaves behind is a
    complete-looking file under a name nothing else claims.
    """
    if not (name.startswith(".") and name.endswith(TMP_SUFFIX)):
        return False
    token = name[: -len(TMP_SUFFIX)].rsplit(".", 1)[-1]
    return len(token) == _TMP_TOKEN_BYTES * 2 and set(token) <= _HEX


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
        tmp = path.parent / _tmp_name(path.name)
        try:
            return os.open(tmp, _TMP_OPEN_FLAGS, creation_mode), tmp
        except FileExistsError:  # pragma: no cover - 64-bit-random collision
            continue


def _resolve_modes(path: Path, mode: int | None) -> tuple[int | None, int]:
    """The bits the final file must carry, and the bits the tmp is born with.

    When ``mode`` is given, or when an existing target's bits are being
    preserved, the tmp is created owner-only so the contents are never
    momentarily more readable than the final file — this matters for the auth
    token, which is written at ``0o600``. A brand-new modeless file is instead
    created at the default umask mode directly (the kernel applies the umask at
    creation), matching a plain write for non-sensitive files without any
    process-global umask manipulation.
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

    The bytes counterpart of :func:`atomic_write_text`, with the same tmp-file
    and permission handling; an existing target keeps its permission bits.

    Callers that land content they did not author — the sync pull, writing
    bytes fetched from the server — need this rather than ``write_bytes``: a
    truncating write that fails partway leaves a file that is neither the old
    version nor the new one, and nothing downstream can tell the difference.
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
    removed on any failure. The permission handling is
    :func:`_resolve_modes`. Guarantees are atomic-replace only: concurrent
    writers each land a complete file (last replace wins, intermediate
    read-modify-write updates can still be lost), and crash durability remains
    out of scope.
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
