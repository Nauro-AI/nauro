"""Durability barriers: POSIX unchanged; Windows flushes through a GENERIC_WRITE handle and
renames with MOVEFILE_WRITE_THROUGH (no directory flush), on journaled NTFS or ReFS only.
Refusals raise a typed error; per-file flush cost is still owed a Windows job measurement."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path
from typing import Any

from nauro.store._atomic import atomic_write_bytes
from nauro.store.generation_refresh_state import GenerationRefreshEvidenceError

if sys.platform == "win32":
    import msvcrt
WINDOWS = os.name == "nt"
W, U, P = ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p
GENERIC_WRITE, FILE_READ_ATTRIBUTES = 0x40000000, 0x80
FLUSH_ACCESS = GENERIC_WRITE | FILE_READ_ATTRIBUTES
FILE_SHARE_ALL, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT = 0x1 | 0x2 | 0x4, 3, 0x00200000
MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH = 0x1, 0x8
DURABLE_FILE_SYSTEMS = frozenset({"NTFS", "ReFS"})
_NOT_FOUND = frozenset({2, 3})
_REASONS = {5: "access is denied or the file is read-only", 32: "another process has it open"}
_INVALID_HANDLE, _BUFFER = ctypes.c_void_p(-1).value, 261


class DurabilityUnavailableError(GenerationRefreshEvidenceError):
    code = "generation_durability_unavailable"


class DurableRenameError(DurabilityUnavailableError, OSError):
    """A write-through rename was refused; still an OSError for rename callers."""


def _kernel32() -> Any:
    if sys.platform == "win32":
        return ctypes.WinDLL("kernel32", use_last_error=True)
    raise DurabilityUnavailableError("Windows durability calls are unavailable here.")


def _last_error(reset: bool = False) -> int:
    if sys.platform == "win32":
        return ctypes.set_last_error(0) if reset else ctypes.get_last_error()
    return 0


def _call(name: str, argtypes: list[Any], restype: Any, *args: Any) -> tuple[Any, int]:
    function = getattr(_kernel32(), name)
    function.argtypes, function.restype = argtypes, restype
    _last_error(reset=True)
    return function(*args), _last_error()


def _descriptor(handle: int) -> int:
    if sys.platform == "win32":
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    raise DurabilityUnavailableError("Windows durability calls are unavailable here.")


def _os_handle(descriptor: int) -> int:
    if sys.platform == "win32":
        return msvcrt.get_osfhandle(descriptor)
    raise DurabilityUnavailableError("Windows durability calls are unavailable here.")


def open_writable(path: Path) -> int:
    flags = FILE_FLAG_OPEN_REPARSE_POINT
    args = (os.fspath(path), FLUSH_ACCESS, FILE_SHARE_ALL, None, OPEN_EXISTING, flags, None)
    handle, error = _call("CreateFileW", [W, U, U, P, U, U, P], P, *args)
    if handle is None or handle == _INVALID_HANDLE:
        if error in _NOT_FOUND:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", os.fspath(path))
        reason = _REASONS.get(error, f"Windows error {error}")
        raise DurabilityUnavailableError(f"Cannot open {path} for a durable flush: {reason}.")
    try:
        return _descriptor(handle)
    except BaseException:
        _call("CloseHandle", [P], ctypes.c_int, handle)
        raise


def flush_writable(descriptor: int, path: Path) -> None:
    handle = _os_handle(descriptor)
    flushed, error = _call("FlushFileBuffers", [P], ctypes.c_int, handle)
    if not flushed:
        raise DurabilityUnavailableError(f"Flush of {path} refused (Windows error {error}).")


def durable_rename(source: Path, destination: Path, *, replace: bool = True) -> None:
    if not WINDOWS:
        (os.replace if replace else os.rename)(source, destination)
        return
    require_durable_file_system(Path(destination).parent)
    flags = MOVEFILE_WRITE_THROUGH | (MOVEFILE_REPLACE_EXISTING if replace else 0)
    args = (os.fspath(source), os.fspath(destination), flags)
    moved, error = _call("MoveFileExW", [W, W, U], ctypes.c_int, *args)
    if not moved:
        if error in _NOT_FOUND:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", os.fspath(source))
        reason = _REASONS.get(error, f"Windows error {error}")
        raise DurableRenameError(f"Cannot move {source} to {destination} durably: {reason}.")


def durable_write_bytes(path: Path, data: bytes) -> None:
    atomic_write_bytes(path, data, rename=durable_rename)


def require_durable_file_system(directory: Path) -> None:
    if not WINDOWS:
        return
    root, name = ctypes.create_unicode_buffer(_BUFFER), ctypes.create_unicode_buffer(_BUFFER)
    args: tuple[Any, ...] = (os.fspath(directory), root, _BUFFER)
    found, error = _call("GetVolumePathNameW", [W, W, U], ctypes.c_int, *args)
    if not found:
        raise DurabilityUnavailableError(f"Cannot find the volume of {directory} ({error}).")
    args = (root.value, None, 0, None, None, None, name, _BUFFER)
    found, error = _call("GetVolumeInformationW", [W, W, U, P, P, P, W, U], ctypes.c_int, *args)
    if not found:
        raise DurabilityUnavailableError(f"Cannot read the file system of {directory} ({error}).")
    if name.value not in DURABLE_FILE_SYSTEMS:
        raise DurabilityUnavailableError(f"{directory} is on {name.value!r}; need NTFS or ReFS.")
