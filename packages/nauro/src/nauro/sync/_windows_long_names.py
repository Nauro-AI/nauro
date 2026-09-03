"""Private Windows long-name lookup for sync path admission."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_INITIAL_BUFFER_SIZE = 260


def _get_long_path_name(path: str, buffer: ctypes.Array[ctypes.c_wchar], size: int) -> int:
    # get_last_error() reads a ctypes-private copy that only a use_last_error
    # handle refreshes, so ctypes.windll would report a stale error code.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    function = kernel32.GetLongPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return int(function(path, buffer, size))


def _existing_long_component(parent: Path, native_component: str) -> str | None:
    if sys.platform != "win32":
        return native_component

    exact_path = os.fspath(parent / native_component)
    size = _INITIAL_BUFFER_SIZE
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        ctypes.set_last_error(0)
        length = _get_long_path_name(exact_path, buffer, size)
        if length == 0:
            error = ctypes.get_last_error()
            if error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                return None
            raise OSError(error, "Windows long-name lookup failed")
        if length >= size:
            size = length + 1
            continue
        return Path(buffer.value).name
