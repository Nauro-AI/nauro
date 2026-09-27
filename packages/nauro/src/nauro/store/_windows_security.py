"""Owner-only access control lists on Windows, where mode bits set no access control.

Owner: the user, SYSTEM or Administrators; allow entries (inherited too) name those or OWNER
RIGHTS (S-1-3-4, the owner alias CPython's mkdir(mode=0o700) writes); deny entries are skipped;
NULL or other allow types refuse. A directory must pass an allowed entry to new files, so store
files are owner-only at creation, then protected before any secret byte. Docs: aclapi,
securitybaseapi, OpenProcessToken, sddl (learn.microsoft.com/windows/win32/api). Stdlib only.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

WINDOWS = os.name == "nt"
SYSTEM, ADMINISTRATORS, OWNER_RIGHTS = "S-1-5-18", "S-1-5-32-544", "S-1-3-4"
ACCESS_ALLOWED, DENY_TYPES = 0, frozenset({1, 6, 10, 12})
PLAIN_TYPES, OBJECT_TYPES = frozenset({0, 1, 9, 10}), frozenset({5, 6, 11, 12})
INHERIT_TO_CHILDREN, INHERITED = 0x1 | 0x2, 0x10
FILE_ALL_ACCESS, ACL_REVISION, SE_FILE_OBJECT = 0x1F01FF, 2, 1
OWNER_INFO, DACL_INFO, PROTECTED_DACL_INFO = 0x1, 0x4, 0x80000000
TOKEN_QUERY, TOKEN_USER, P, U = 0x8, 1, ctypes.c_void_p, ctypes.c_uint32
PP = ctypes.POINTER(ctypes.c_void_p)


class OwnerOnlyError(ValueError):
    """A credential path is not owner-only under its Windows access control list."""

    code = "credential_access_not_owner_only"


class Security(NamedTuple):
    owner: str | None
    entries: tuple[tuple[int, int, str | None], ...] | None  # (type, flags, sid); None is NULL


class _Native:
    advapi: Any
    kernel: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OwnerOnlyError("Windows access control is unavailable on this platform.")
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    def _call(self, library: Any, name: str, argtypes: list[Any], restype: Any, *args: Any) -> Any:
        function = getattr(library, name)
        function.argtypes, function.restype = argtypes, restype
        return function(*args)

    def _ok(self, name: str, argtypes: list[Any], *args: Any) -> None:
        if not self._call(self.advapi, name, argtypes, ctypes.c_int, *args):
            error = getattr(ctypes, "get_last_error", lambda: 0)()
            raise OwnerOnlyError(f"{name} failed (Windows error {error}).")

    def _free(self, pointer: Any) -> None:
        self._call(self.kernel, "LocalFree", [P], P, pointer)

    def _sid_text(self, sid: Any) -> str:
        text = ctypes.c_void_p()
        self._ok("ConvertSidToStringSidW", [P, PP], sid, ctypes.byref(text))
        try:
            return ctypes.wstring_at(text.value) if text.value else ""
        finally:
            self._free(text)

    def current_user(self) -> str:
        token, size = ctypes.c_void_p(), U(0)
        process = self._call(self.kernel, "GetCurrentProcess", [], P)
        self._ok("OpenProcessToken", [P, U, PP], process, TOKEN_QUERY, ctypes.byref(token))
        try:
            query = [P, ctypes.c_int, P, U, ctypes.POINTER(U)]
            buffer = ctypes.create_string_buffer(256)  # TOKEN_USER plus a SID of at most 68 bytes
            args = (token, TOKEN_USER, buffer, len(buffer), ctypes.byref(size))
            self._ok("GetTokenInformation", query, *args)
            return self._sid_text(ctypes.cast(buffer, PP).contents)
        finally:
            self._call(self.kernel, "CloseHandle", [P], ctypes.c_int, token)

    def read(self, path: Path) -> Security:
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        argtypes = [ctypes.c_wchar_p, ctypes.c_int, U, PP, PP, PP, PP, PP]
        args = (os.fspath(path), SE_FILE_OBJECT, OWNER_INFO | DACL_INFO, ctypes.byref(owner))
        more = (None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        error = self._call(self.advapi, "GetNamedSecurityInfoW", argtypes, U, *args, *more)
        if error:
            raise OwnerOnlyError(f"Cannot read the access control list of {path} ({error}).")
        try:
            if not dacl.value:
                return Security(self._sid_text(owner), None)
            entries = []
            # ACL header: revision, pad, size (2), count (2); ACE header: type, flags, size (2).
            for index in range(int.from_bytes(ctypes.string_at(dacl.value, 8)[4:6], "little")):
                ace = ctypes.c_void_p()
                self._ok("GetAce", [P, U, PP], dacl, index, ctypes.byref(ace))
                head = ctypes.string_at(ace.value or 0, 4)
                raw = ctypes.string_at(ace.value or 0, int.from_bytes(head[2:4], "little"))
                offset = sid_offset(raw)
                start = ctypes.c_void_p((ace.value or 0) + (offset or 0))
                entries.append((raw[0], raw[1], None if offset is None else self._sid_text(start)))
            return Security(self._sid_text(owner), tuple(entries))
        finally:
            self._free(descriptor)

    def write(self, path: Path, sids: tuple[str, ...], flags: int) -> None:
        pointers = []
        try:
            for text in sids:
                sid = ctypes.c_void_p()
                self._ok("ConvertStringSidToSidW", [ctypes.c_wchar_p, PP], text, ctypes.byref(sid))
                pointers.append(sid)
            lengths = [self._call(self.advapi, "GetLengthSid", [P], U, s) for s in pointers]
            acl = ctypes.create_string_buffer((8 + sum(8 + n for n in lengths) + 3) // 4 * 4)
            self._ok("InitializeAcl", [P, U, U], acl, len(acl), ACL_REVISION)
            for sid in pointers:
                args = (acl, ACL_REVISION, flags, FILE_ALL_ACCESS, sid)
                self._ok("AddAccessAllowedAceEx", [P, U, U, U, P], *args)
            argtypes = [ctypes.c_wchar_p, ctypes.c_int, U, P, P, P, P]
            info = DACL_INFO | PROTECTED_DACL_INFO
            target = (os.fspath(path), SE_FILE_OBJECT, info, None, None, acl, None)
            error = self._call(self.advapi, "SetNamedSecurityInfoW", argtypes, U, *target)
            if error:
                raise OwnerOnlyError(f"Cannot set the access control list of {path} ({error}).")
        finally:
            for sid in pointers:
                self._free(sid)


def sid_offset(ace: bytes) -> int | None:
    if ace[0] in PLAIN_TYPES:
        return 8
    if ace[0] not in OBJECT_TYPES:
        return None
    flags = int.from_bytes(ace[8:12], "little")
    return 12 + 16 * (flags & 0x1) + 16 * ((flags & 0x2) >> 1)


def _api() -> Any:
    return _Native()


def require_owner_only(path: Path, *, directory: bool = False) -> None:
    api = _api()
    owners = {api.current_user(), SYSTEM, ADMINISTRATORS}
    allowed = owners | {OWNER_RIGHTS}
    found = api.read(path)
    if found.entries is None:
        raise OwnerOnlyError(f"{path} has a NULL access control list, which grants everyone.")
    if found.owner not in owners:
        raise OwnerOnlyError(f"{path} is owned by {found.owner}, not the current user.")
    for kind, _flags, sid in found.entries:
        if kind in DENY_TYPES:
            continue
        if kind != ACCESS_ALLOWED or sid not in allowed:
            raise OwnerOnlyError(f"{path} grants access to {sid or kind}, not only the owner.")
    inherits = any(kind == ACCESS_ALLOWED and flags & 0x1 for kind, flags, _ in found.entries)
    if directory and not inherits:
        raise OwnerOnlyError(f"{path} passes no owner-only list to new files.")


def set_owner_only(path: Path, *, directory: bool = False) -> None:
    api = _api()
    sids = (api.current_user(), SYSTEM, ADMINISTRATORS)
    api.write(path, sids, INHERIT_TO_CHILDREN if directory else 0)
