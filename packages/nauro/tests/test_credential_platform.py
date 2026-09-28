from __future__ import annotations

import builtins
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import SoftFileLock

from nauro.store import _platform_durability as durability
from nauro.store import _windows_security as security
from nauro.store import home as home_module
from nauro.store import replica_control
from nauro.store.replica_control import ReplicaControlBusyError, ReplicaControlReadError
from nauro.sync import reference_credentials as credentials_module
from nauro.sync.decision_profile import _private_json
from nauro.sync.generation_credentials import AccountStore, GenerationAuth
from nauro.sync.reference_credentials import CredentialRecord, CredentialStore

NATIVE = os.name == "nt"
USER = "S-1-5-21-1000-2000-3000-1001"
EVERYONE = "S-1-1-0"
OWNER_ONLY = (USER, security.SYSTEM, security.ADMINISTRATORS)
CPYTHON_MKDIR = tuple((0, 3, sid) for sid in ("S-1-5-18", "S-1-5-32-544", "S-1-3-4"))
ACTOR = "01K" + "0" * 21 + "08"


class FakeSecurity:
    """Stands in for advapi32; lists are keyed by file identity so they follow a rename."""

    def __init__(self) -> None:
        self.lists: dict[tuple[int, int], security.Security] = {}
        self.writes: list[tuple[str, tuple[str, ...], int]] = []
        self.fail = False

    def current_user(self) -> str:
        return USER

    def _key(self, path) -> tuple[int, int]:
        info = os.stat(path)
        return info.st_dev, info.st_ino

    def read(self, path) -> security.Security:
        unset = security.Security(USER, ((0, 0, USER), (0, 0, EVERYONE)))
        return self.lists.get(self._key(path), unset)

    def write(self, path, sids, flags) -> None:
        if self.fail:
            raise security.OwnerOnlyError("SetNamedSecurityInfoW failed (Windows error 5).")
        if not os.path.isdir(path):
            assert os.path.getsize(path) == 0, "access list set after bytes were written"
        self.writes.append((Path(path).name, sids, flags))
        entries = tuple((security.ACCESS_ALLOWED, flags, sid) for sid in sids)
        self.lists[self._key(path)] = security.Security(sids[0], entries)

    def change(self, path, *, owner=None, entry=None, null=False) -> None:
        found = self.read(path)
        entries = None if null else (*found.entries, *([entry] if entry else []))
        self.lists[self._key(path)] = security.Security(owner or found.owner, entries)


@pytest.fixture
def fake(monkeypatch):
    api = FakeSecurity()
    monkeypatch.setattr(durability, "WINDOWS", True)
    monkeypatch.setattr(security, "WINDOWS", True)
    monkeypatch.setattr(security, "_api", lambda: api)
    if not NATIVE:
        rename = lambda source, target, replace=True: os.replace(source, target)  # noqa: E731
        monkeypatch.setattr(durability, "durable_rename", rename)
    return api


@pytest.fixture
def store(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    home = home_module.ensure_nauro_home()
    return CredentialStore(home / "credentials.json", "binding", ACTOR)


def _record(revision: str = "r1") -> CredentialRecord:
    return CredentialRecord(
        revision=revision,
        binding="binding",
        state="active",
        user_id=ACTOR,
        access_token="access",
        refresh_token="refresh",
        expires_at=1,
    )


def _paths(store):
    lock = store.path.with_name(store.path.name + ".lock")
    return {
        "home": store.path.parent,
        "credential": store.path,
        "lock": lock,
        "marker": store.marker,
    }


def _renewing(store) -> None:
    with store.locked():
        store.begin()
        store.write(_record())


def _link_or_skip(link, target) -> None:
    try:
        link.symlink_to(target, target_is_directory=Path(target).is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _owner_only(flags: int) -> tuple:
    return tuple((security.ACCESS_ALLOWED, flags, sid) for sid in OWNER_ONLY)


def test_windows_branch_is_selected_by_os_name():
    assert security.WINDOWS is durability.WINDOWS is (os.name == "nt")


def test_windows_lock_uses_the_native_control_lock(store, monkeypatch):
    calls = []
    real = credentials_module._native_control_lock

    def spy(parent, lock, timeout):
        calls.append((parent, lock.name, timeout))
        return real(parent, lock, timeout)

    monkeypatch.setattr(credentials_module, "_native_control_lock", spy)
    with store.locked(timeout=0.5):
        pass
    assert calls == [(store.path.parent, "credentials.json.lock", 0.5)]


def test_windows_busy_lock_maps_to_busy_value_error(store):
    with store.locked():
        other = CredentialStore(store.path, "binding", ACTOR)
        busy = pytest.raises(ValueError, match="Reference credentials busy")
        with busy, other.locked(timeout=0.05):
            pytest.fail("Entered a held credential lock")
    with store.locked():
        pass


def test_windows_body_errors_are_not_remapped(store):
    with pytest.raises(ReplicaControlBusyError, match="inner"), store.locked():
        raise ReplicaControlBusyError("inner")
    with pytest.raises(ReplicaControlReadError, match="inner"), store.locked():
        raise ReplicaControlReadError("inner")


def test_windows_soft_file_lock_is_refused(store, monkeypatch):
    monkeypatch.setattr(replica_control, "FileLock", SoftFileLock)
    with pytest.raises(ValueError, match="Unsafe credential lock") as raised, store.locked():
        pytest.fail("Entered through a soft file lock")
    assert isinstance(raised.value.__cause__, ReplicaControlReadError)


def test_windows_linked_lock_file_is_refused(store, tmp_path):
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(b"")
    _link_or_skip(_paths(store)["lock"], target)
    with pytest.raises(ValueError, match="Unsafe credential lock"), store.locked():
        pytest.fail("Entered through a linked lock")


def test_windows_store_sets_owner_only_lists_at_creation(store, fake):
    _renewing(store)
    names = [name for name, _, _ in fake.writes]
    assert names[:3] == ["home", "credentials.json.lock", "credentials.json.renewing"]
    assert names[3].startswith(".credentials.json.") and names[3].endswith(".tmp")
    assert len(names) == 4
    assert {(sids, flags) for _, sids, flags in fake.writes[1:]} == {(OWNER_ONLY, 0)}
    assert fake.writes[0][1:] == (OWNER_ONLY, security.INHERIT_TO_CHILDREN)
    paths = _paths(store)
    assert fake.read(paths["home"]).entries == _owner_only(security.INHERIT_TO_CHILDREN)
    for name in ("credential", "lock", "marker"):
        assert fake.read(paths[name]) == security.Security(USER, _owner_only(0))
    with store.locked():
        assert store.incomplete() is True
        assert store.read() == _record()
        store.finish()
    assert len(fake.writes) == 4


@pytest.mark.parametrize("target", ["home", "credential", "lock", "marker"])
@pytest.mark.parametrize(
    "entry",
    [
        (security.ACCESS_ALLOWED, 0, EVERYONE),
        (security.ACCESS_ALLOWED, security.INHERITED, EVERYONE),
        (security.ACCESS_ALLOWED, security.INHERITED, "S-1-5-11"),
        (5, 0, None),
    ],
    ids=["explicit", "inherited", "authenticated-users", "object-entry"],
)
def test_windows_extra_allow_entry_refuses(store, fake, target, entry):
    _renewing(store)
    fake.change(_paths(store)[target], entry=entry)
    with pytest.raises(security.OwnerOnlyError, match="grants access"), store.locked():
        store.read()
        store.incomplete()


@pytest.mark.parametrize("target", ["home", "credential", "lock", "marker"])
def test_windows_foreign_owner_or_null_list_refuses(store, fake, target):
    _renewing(store)
    path = _paths(store)[target]
    fake.change(path, owner=EVERYONE)
    with pytest.raises(security.OwnerOnlyError, match="owned by"), store.locked():
        store.read()
        store.incomplete()
    fake.change(path, owner=USER, null=True)
    with pytest.raises(security.OwnerOnlyError, match="NULL"), store.locked():
        store.read()
        store.incomplete()


@pytest.mark.parametrize("kind", [1, 6, 10, 12])
def test_windows_deny_entries_only_remove_access(store, fake, kind):
    _renewing(store)
    for path in _paths(store).values():
        fake.change(path, entry=(kind, security.INHERITED, EVERYONE))
    with store.locked():
        assert store.read() == _record()
        assert store.incomplete() is True


def test_object_ace_sid_offsets_follow_the_documented_layouts():
    everyone = bytes([1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0])
    both = bytes([6, 0, 56, 0]) + bytes(4) + (3).to_bytes(4, "little") + bytes(32) + everyone
    assert security.sid_offset(both) == 44 and both[44:] == everyone
    assert security.sid_offset(bytes([5, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0])) == 28
    assert security.sid_offset(bytes([12, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])) == 12
    assert [security.sid_offset(bytes([kind]) + bytes(11)) for kind in (0, 1, 9, 10, 7)] == [
        8,
        8,
        8,
        8,
        None,
    ]


def test_windows_cpython_mkdir_list_is_owner_only(tmp_path, monkeypatch, fake):
    home = tmp_path / "home"
    os.mkdir(home, 0o700)
    fake.lists[fake._key(home)] = security.Security(USER, CPYTHON_MKDIR)
    store = CredentialStore(home / "credentials.json", "binding", ACTOR)
    _renewing(store)
    with store.locked():
        assert store.read() == _record()
    fake.change(home, owner="S-1-3-4")
    with pytest.raises(security.OwnerOnlyError, match="owned by"), store.locked():
        pytest.fail("OWNER RIGHTS is an alias, never an owner")


def test_windows_home_that_passes_nothing_to_new_files_refuses(store, fake):
    fake.lists[fake._key(store.path.parent)] = security.Security(USER, _owner_only(0))
    with pytest.raises(security.OwnerOnlyError, match="no owner-only list"), store.locked():
        pytest.fail("Entered a home whose new files get the default list")


def test_windows_home_is_created_with_the_restricted_mode(tmp_path, monkeypatch, fake):
    modes = []
    real = Path.mkdir

    def mkdir(self, mode=0o777, *args, **kwargs):
        modes.append(mode)
        return real(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    home_module.ensure_nauro_home()
    assert modes[0] == 0o700


def test_windows_failed_marker_list_leaves_no_marker(store, fake):
    with store.locked():
        fake.fail = True
        with pytest.raises(security.OwnerOnlyError):
            store.begin()
        assert not store.marker.exists()
        fake.fail = False
        store.begin()
        assert store.incomplete() is True


def test_windows_linked_home_refuses(tmp_path, monkeypatch, fake):
    real = tmp_path / "real"
    real.mkdir()
    security.set_owner_only(real, directory=True)
    _link_or_skip(tmp_path / "linked", real)
    leaf = CredentialStore(tmp_path / "linked" / "credentials.json", "binding", ACTOR)
    with pytest.raises(ValueError, match="owner-only directory"), leaf.locked():
        pytest.fail("Entered a linked home")
    (real / "home").mkdir()
    security.set_owner_only(real / "home", directory=True)
    nested = CredentialStore(tmp_path / "linked" / "home" / "c.json", "binding", ACTOR)
    with pytest.raises(security.OwnerOnlyError, match="link"), nested.locked():
        pytest.fail("Entered a home under a linked ancestor")
    assert fake.writes == [("real", OWNER_ONLY, 3), ("home", OWNER_ONLY, 3)]


def test_windows_junction_home_refuses(store, monkeypatch):
    real_lstat = os.lstat
    parent = os.fspath(store.path.parent)

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if os.fspath(path) != parent:
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)

    monkeypatch.setattr(os, "lstat", lstat)
    assert stat.S_ISDIR(store.path.parent.lstat().st_mode)
    with pytest.raises(security.OwnerOnlyError, match="link"), store.locked():
        pytest.fail("Entered a junction home")


def test_windows_home_creation_is_owner_only_once(tmp_path, monkeypatch, fake):
    home = tmp_path / "home"
    monkeypatch.setenv("NAURO_HOME", str(home))
    assert home_module.ensure_nauro_home() == home
    assert home_module.ensure_nauro_home() == home
    assert fake.writes == [("home", OWNER_ONLY, security.INHERIT_TO_CHILDREN)]
    fake.fail = True
    assert home_module.ensure_nauro_home() == home
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "other"))
    with pytest.raises(security.OwnerOnlyError, match="SetNamedSecurityInfoW"):
        home_module.ensure_nauro_home()
    assert not (tmp_path / "other").exists()
    (tmp_path / "file").write_bytes(b"")
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "file"))
    with pytest.raises(FileExistsError):
        home_module.ensure_nauro_home()


def test_home_removal_failure_keeps_the_access_refusal(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    fake.fail = True
    monkeypatch.setattr(Path, "rmdir", lambda self: (_ for _ in ()).throw(OSError("in use")))
    with pytest.raises(security.OwnerOnlyError, match="SetNamedSecurityInfoW"):
        home_module.ensure_nauro_home()


def _private(tmp_path, content: bytes) -> Path:
    path = tmp_path / "private.json"
    path.touch()
    security.set_owner_only(path)
    path.write_bytes(content)
    return path


def test_windows_private_json_reads_an_owner_only_file(tmp_path, fake):
    assert _private_json(_private(tmp_path, b'{"a":\r\n1}')) == {"a": 1}
    with pytest.raises(FileNotFoundError):
        _private_json(tmp_path / "missing.json")


def test_windows_private_json_refuses_a_link(tmp_path, fake):
    path = _private(tmp_path, b"{}")
    _link_or_skip(tmp_path / "link.json", path)
    with pytest.raises(ValueError, match="regular file"):
        _private_json(tmp_path / "link.json")


def test_windows_private_json_refuses_a_directory(tmp_path, fake):
    (tmp_path / "directory.json").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        _private_json(tmp_path / "directory.json")


def test_windows_private_json_refuses_a_swap_during_open(tmp_path, monkeypatch, fake):
    path = _private(tmp_path, b"{}")
    (tmp_path / "other").mkdir()
    swapped = _private(tmp_path / "other", b'{"swapped":true}')
    real_open = os.open

    def swapping(target, *args, **kwargs):
        if Path(target) == path:
            os.replace(swapped, path)
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping)
    with pytest.raises(ValueError, match="changed during open"):
        _private_json(path)


def test_windows_private_json_refuses_a_swap_during_the_list_check(tmp_path, monkeypatch, fake):
    path = _private(tmp_path, b'{"foreign":true}')
    fake.change(path, entry=(security.ACCESS_ALLOWED, 0, EVERYONE))
    (tmp_path / "other").mkdir()
    swapped = _private(tmp_path / "other", b"{}")
    real_read = fake.read

    def swapping(target):
        if Path(target) == path and swapped.exists():
            os.replace(swapped, path)
        return real_read(target)

    monkeypatch.setattr(fake, "read", swapping)
    with pytest.raises(ValueError, match="changed during open"):
        _private_json(path)


def test_windows_private_json_refuses_oversize_and_foreign_lists(tmp_path, fake):
    path = _private(tmp_path, b" " * 65537)
    with pytest.raises(ValueError, match="size limit"):
        _private_json(path)
    path.write_bytes(b"{}")
    fake.change(path, entry=(security.ACCESS_ALLOWED, 0, EVERYONE))
    with pytest.raises(security.OwnerOnlyError, match="grants access"):
        _private_json(path)


def test_windows_status_never_imports_fcntl(tmp_path, monkeypatch, store):
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fcntl" and (globals or {}).get("__name__") == credentials_module.__name__:
            raise ModuleNotFoundError("No module named 'fcntl'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    auth = object.__new__(GenerationAuth)
    auth.store = AccountStore(store.path.with_name("generation-credentials-x.json"), "x")
    assert auth.status() == "logged_out"
    _renewing(store)
    with store.locked():
        store.finish()
        assert store.read() == _record()
    monkeypatch.setattr(durability, "WINDOWS", False)
    posix_only = pytest.raises(ValueError if NATIVE else ModuleNotFoundError, match="fcntl|POSIX")
    with posix_only, store.locked():
        pytest.fail("The POSIX branch ran without fcntl")


@pytest.mark.skipif(NATIVE, reason="the POSIX branch needs flock and owner mode bits")
def test_posix_store_never_reaches_windows_code(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Windows access control reached on POSIX")

    monkeypatch.setattr(security, "_api", forbidden)
    monkeypatch.setattr(credentials_module, "_native_control_lock", forbidden)
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    home = home_module.ensure_nauro_home()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    store = CredentialStore(home / "credentials.json", "binding", ACTOR)
    _renewing(store)
    with store.locked():
        assert store.read() == _record()
        store.finish()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only regular file"):
        _private_json(store.path)
    home.chmod(0o755)
    with pytest.raises(ValueError, match="owner-only directory"), store.locked():
        pytest.fail("Entered a group-readable home")


def _icacls(*args: str) -> None:
    subprocess.run(["icacls", *args], check=True, capture_output=True)


@pytest.mark.skipif(not NATIVE, reason="real Windows access control lists")
def test_native_store_lists_are_exactly_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    store = CredentialStore(home_module.ensure_nauro_home() / "c.json", "binding", ACTOR)
    _renewing(store)
    user = security._api().current_user()
    for path in _paths(store).values():
        found = security._api().read(path)
        assert found.owner in {user, security.SYSTEM, security.ADMINISTRATORS}
        assert sorted(sid for _, _, sid in found.entries) == sorted(
            (user, security.SYSTEM, security.ADMINISTRATORS)
        )
        assert {kind for kind, _, _ in found.entries} == {security.ACCESS_ALLOWED}
    with store.locked():
        assert store.read() == _record()
        store.finish()


@pytest.mark.skipif(not NATIVE, reason="real Windows access control lists")
def test_native_granted_and_inherited_access_refuses(tmp_path):
    path = tmp_path / "file.json"
    path.write_bytes(json.dumps({}).encode())
    security.set_owner_only(path)
    security.require_owner_only(path)
    _icacls(str(path), "/grant", "*S-1-1-0:R")
    with pytest.raises(security.OwnerOnlyError, match=EVERYONE):
        security.require_owner_only(path)
    shared = tmp_path / "shared"
    shared.mkdir()
    security.set_owner_only(shared, directory=True)
    _icacls(str(shared), "/grant", "*S-1-1-0:(OI)(CI)R")
    (shared / "child.json").write_bytes(b"{}")
    inherited = security._api().read(shared / "child.json").entries
    assert any(flags & security.INHERITED and sid == EVERYONE for _, flags, sid in inherited)
    with pytest.raises(security.OwnerOnlyError, match=EVERYONE):
        security.require_owner_only(shared / "child.json")


@pytest.mark.skipif(not NATIVE, reason="CPython's owner-only mkdir list")
def test_native_cpython_mkdir_directory_is_owner_only(tmp_path):
    os.mkdir(tmp_path / "home", 0o700)
    security.require_owner_only(tmp_path / "home")


@pytest.mark.skipif(not NATIVE, reason="directory junctions")
def test_native_junction_home_refuses(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    security.set_owner_only(real, directory=True)
    link = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True)
    with pytest.raises(ValueError), CredentialStore(link / "c.json", "b", ACTOR).locked():
        pytest.fail("Entered a junction home")
