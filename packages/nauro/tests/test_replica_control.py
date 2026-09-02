from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock, SoftFileLock, Timeout

from nauro.store import replica_control as control
from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    FlatProjectAuthority,
    GenerationControlCorruptError,
    RefreshRequiredError,
    ReplicaActorMismatchError,
)
from nauro.store.replica_control import (
    ReplicaControlBusyError,
    ReplicaControlReadError,
    locked_replica_control_snapshot,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
USER_ID = "01K33333333333333333333333"
SCOPE_ID = "b" * 64
BOUNDARY_IDS = "store lock root marker actor pointer".split()


class HostileStr(str):
    def __str__(self) -> str:
        raise AssertionError("hostile string was converted")


def _binding(path: Path, mode="cloud") -> ResolvedProjectBinding:
    path.mkdir(parents=True)
    url = "https://mcp.nauro.ai" if mode == "cloud" else None
    return ResolvedProjectBinding(path, PROJECT_ID, "Nauro", mode, url)


def _marker(**changes: object) -> bytes:
    value: dict[str, object] = {
        "schema_version": 1,
        "authority": "generation",
        "project_id": PROJECT_ID,
        "store_format_version": 1,
    }
    return json.dumps(value | changes).encode()


def _pointer(**changes: object) -> bytes:
    value: dict[str, object] = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "store_format_version": 1,
        "generation_id": "01K11111111111111111111111",
        "manifest_digest": "a" * 64,
        "committed_at": "2026-09-02T01:02:03.000004Z",
        "installed_at": "2026-09-02T01:03:04.000005Z",
        "installed_state_id": "01K22222222222222222222222",
        "installed_for_user_id": USER_ID,
        "projection_class": "contributor_plus",
        "projection_scope_id": SCOPE_ID,
    }
    return json.dumps(value | changes).encode()


def _layout(path: Path):
    return SimpleNamespace(
        control_lock=path / ".replica-control.lock",
        control_root=path / ".replica",
        authority_marker=path / ".replica/authority.json",
    )


def _pointer_path(path: Path) -> Path:
    return path / ".replica" / "v1" / "actors" / USER_ID / "pointer.json"


def _write_control(path: Path, marker: bytes | None = None, pointer: bytes | None = None):
    marker, pointer = marker or _marker(), pointer or _pointer()
    layout = _layout(path)
    layout.control_root.mkdir()
    layout.authority_marker.write_bytes(marker)
    _pointer_path(path).parent.mkdir(parents=True)
    _pointer_path(path).write_bytes(pointer)
    return marker, pointer


def _locked(binding: ResolvedProjectBinding, **changes: object):
    options: dict[str, object] = dict(active_user_id=USER_ID, active_projection_scope_id=SCOPE_ID)
    options.update(changes)
    return locked_replica_control_snapshot(binding, **options)  # type: ignore[arg-type]


def _assert_error(binding: ResolvedProjectBinding, error, **kw):
    with pytest.raises(error) as raised, _locked(binding, **kw):
        pass
    assert (type(raised.value), raised.value.code) == (error, error.code)


def test_local_mode_performs_no_lock_or_control_io(tmp_path, monkeypatch):
    binding = _binding(tmp_path / PROJECT_ID, "local")
    monkeypatch.setattr(control, "_validate_store_path", lambda *_: pytest.fail("path I/O"))
    monkeypatch.setattr(control, "_new_native_lock", lambda *_: pytest.fail("lock I/O"))
    with _locked(binding, active_user_id=HostileStr(USER_ID)) as snapshot:
        assert snapshot.authority == FlatProjectAuthority(binding)
        assert (snapshot.marker_json, snapshot.pointer_json) == (None, None)
    assert not any(path.exists() for path in vars(_layout(binding.store_path)).values())


def test_absent_marker_ignores_a_poisoned_dormant_pointer(tmp_path, monkeypatch):
    binding = _binding(tmp_path / PROJECT_ID)
    pointer = _pointer_path(binding.store_path)
    pointer.parent.mkdir(parents=True)
    _symlink(pointer, tmp_path / "outside")
    monkeypatch.setattr(control, "_actor_pointer", lambda *_: pytest.fail("pointer path"))
    with _locked(binding) as snapshot:
        assert snapshot.authority == FlatProjectAuthority(binding)
        assert (snapshot.marker_json, snapshot.pointer_json) == (None, None)


@pytest.mark.parametrize(
    ("marker", "actor", "error", "pointer_allowed"),
    [
        (b"not-json", USER_ID, GenerationControlCorruptError, False),
        (_marker(project_id="01K" + "0" * 23), USER_ID, GenerationControlCorruptError, False),
        (_marker(store_format_version=2), USER_ID, ClientUpgradeRequiredError, False),
        (_marker(), HostileStr(USER_ID), ReplicaActorMismatchError, False),
        (_marker(), USER_ID, RefreshRequiredError, True),
    ],
)
def test_control_failure_order(tmp_path, monkeypatch, marker, actor, error, pointer_allowed):
    binding = _binding(tmp_path / PROJECT_ID)
    _layout(binding.store_path).control_root.mkdir()
    _layout(binding.store_path).authority_marker.write_bytes(marker)
    if not pointer_allowed:
        monkeypatch.setattr(control, "_actor_pointer", lambda *_: pytest.fail("pointer path"))
    _assert_error(binding, error, active_user_id=actor)


def test_snapshot_retains_exact_bytes_and_immutable_complete_authority(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    marker, pointer = _write_control(binding.store_path, _marker() + b"\n", _pointer() + b"\n")
    with _locked(binding) as snapshot:
        assert (snapshot.marker_json, snapshot.pointer_json) == (marker, pointer)
        assert snapshot.authority.pointer.model_dump() == json.loads(pointer)
        assert not vars(snapshot).keys() & {"_active", "_guard"}
        with pytest.raises(FrozenInstanceError):
            snapshot.pointer_json = b"{}"


def test_reread_holds_lock_until_complete_and_expires_after_exit(tmp_path, monkeypatch):
    binding = _binding(tmp_path / PROJECT_ID)
    _write_control(binding.store_path)
    real_guard, interrupted = threading.Lock(), threading.Event()
    interruption = KeyboardInterrupt("exit interrupted")

    class InterruptingLock:
        def __enter__(self):
            if real_guard.locked() and not interrupted.is_set():
                interrupted.set()
                raise interruption
            real_guard.acquire()

        def __exit__(self, *_):
            real_guard.release()

    monkeypatch.setattr(control, "Lock", InterruptingLock)
    snapshot = (manager := _locked(binding)).__enter__()
    entered, proceed, exited = (threading.Event() for _ in range(3))

    def paused_read(path: Path, read=control._read_optional_file):
        entered.set()
        assert proceed.wait(5)
        return read(path)

    monkeypatch.setattr(control, "_actor_pointer", lambda *_: pytest.fail("new path"))
    monkeypatch.setattr(control, "_read_optional_file", paused_read)
    result, observed = [], []
    rereader = threading.Thread(target=lambda: result.append(snapshot.reread_authority()))

    def observe_exit() -> None:
        try:
            assert interrupted.wait(5)
            observed.append(not exited.wait(0.1))
            observed.append(_assert_error(binding, ReplicaControlBusyError, timeout=0) is None)
        finally:
            proceed.set()

    observer = threading.Thread(target=observe_exit)
    rereader.start()
    assert entered.wait(5)
    observer.start()
    with pytest.raises(KeyboardInterrupt) as raised:
        manager.__exit__(None, None, None)
    exited.set()
    rereader.join(5)
    observer.join(5)
    assert (result, observed) == ([snapshot.authority], [True, True])
    assert raised.value is interruption
    path = _layout(binding.store_path).control_lock
    assert path.is_file()
    with FileLock(str(path), timeout=0):
        pass
    for reread in (snapshot.reread_authority, snapshot._reread):
        with pytest.raises(ReplicaControlReadError) as raised:
            reread()
        assert raised.value.code == "generation_control_unavailable"


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl inode identity test")
def test_waiter_and_contender_share_the_stable_lock_inode(tmp_path: Path) -> None:
    import fcntl

    binding = _binding(tmp_path / PROJECT_ID)
    path = _layout(binding.store_path).control_lock
    ready, released, acquired, done = (threading.Event() for _ in range(4))

    def waiter() -> None:
        descriptor = os.open(path, os.O_RDWR)
        ready.set()
        released.wait()
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        acquired.set()
        done.wait()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    thread = threading.Thread(target=waiter)
    try:
        with _locked(binding):
            thread.start()
            assert ready.wait(5)
            released.set()
        assert acquired.wait(5)
        with pytest.raises(Timeout):
            FileLock(str(path)).acquire(timeout=0)
    finally:
        released.set()
        done.set()
        thread.join(5)
    assert not thread.is_alive()


class _AcquireFailure:
    def acquire(self) -> None:
        raise OSError("acquire failed")


def _broken_release_lock(path: Path, timeout: float):
    lock = FileLock(str(path), timeout=timeout)
    release = lock.release

    def fail(force: bool = False) -> None:
        release(force=force)
        if not force:
            raise OSError("release failed")

    lock.release = fail  # type: ignore[method-assign]
    return lock


def test_lock_acquire_and_release_failures_are_typed(tmp_path, monkeypatch):
    binding = _binding(tmp_path / PROJECT_ID)
    monkeypatch.setattr(control, "FileLock", SoftFileLock)
    _assert_error(binding, ReplicaControlReadError)
    _layout(binding.store_path).control_lock.touch()
    seen = []
    fallback = SimpleNamespace(acquire=lambda: seen.append(1), release=lambda: None)
    monkeypatch.setattr(control, "_new_native_lock", lambda *_: fallback)
    _assert_error(binding, ReplicaControlReadError)
    assert seen == [1]
    monkeypatch.setattr(control, "_new_native_lock", lambda *_: _AcquireFailure())
    _assert_error(binding, ReplicaControlReadError)
    monkeypatch.setattr(control, "_new_native_lock", _broken_release_lock)
    _assert_error(binding, ReplicaControlReadError)
    with pytest.raises(RuntimeError, match="caller failed"), _locked(binding):
        raise RuntimeError("caller failed")


def _symlink(link: Path, target: Path, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("link creation is unavailable")


@pytest.mark.parametrize("kind", ["link", "reparse"])
@pytest.mark.parametrize("target", range(6), ids=BOUNDARY_IDS)
def test_managed_boundaries_reject_links_and_reparse(tmp_path, monkeypatch, kind, target):
    home = tmp_path / "managed"
    monkeypatch.setenv("NAURO_HOME", str(home))
    store = home / "projects" / PROJECT_ID
    store.parent.mkdir(parents=True)
    store.mkdir()
    binding = ResolvedProjectBinding(store, PROJECT_ID, "Nauro", "cloud", "https://mcp.nauro.ai")
    layout, pointer = _layout(store), _pointer_path(store)
    boundary = (store, *vars(layout).values(), pointer.parent, pointer)[target]
    if kind == "reparse":
        _write_control(store)
        layout.control_lock.touch()
        original = os.lstat

        def lstat(path):
            metadata = original(path)
            if Path(path) == boundary:
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata

        monkeypatch.setattr(control.os, "lstat", lstat)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(_pointer())
        if target == 0:
            store.rmdir()
        elif target >= 3:
            boundary.parent.mkdir(parents=True)
        if target >= 4:
            layout.authority_marker.write_bytes(_marker())
        directory = target in {0, 2, 4}
        _symlink(boundary, outside if directory else outside / "file", directory)
    with pytest.raises(ReplicaControlReadError) as raised, _locked(binding):
        pass
    assert raised.value.code == "generation_control_unavailable"


@pytest.mark.parametrize("home", ["relative", "linked"])
def test_store_path_rules(tmp_path, monkeypatch, home):
    monkeypatch.chdir(tmp_path)
    if home == "linked":
        Path("target").mkdir()
        _symlink(Path(home), Path("target"), True)
    monkeypatch.setenv("NAURO_HOME", home)
    binding = _binding(control.get_store_path_v2(PROJECT_ID))
    with _locked(binding) as snapshot:
        assert snapshot.authority == FlatProjectAuthority(binding)
    expected = tmp_path / "managed" / PROJECT_ID
    monkeypatch.setattr(control, "get_store_path_v2", lambda _: expected)
    for path in [
        Path(PROJECT_ID),
        tmp_path / "wrong",
        tmp_path / "parent/../" / PROJECT_ID,
        expected.parent / "nested" / PROJECT_ID,
    ]:
        binding = ResolvedProjectBinding(path, PROJECT_ID, "Nauro", "cloud", "https://mcp.nauro.ai")
        with pytest.raises(ReplicaControlReadError), _locked(binding):
            pass


@pytest.mark.parametrize("kind", ["limit", "large", "directory", "fifo"])
def test_control_files_are_bounded_and_regular(tmp_path: Path, kind: str) -> None:
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    binding = _binding(tmp_path / PROJECT_ID)
    layout, marker = _layout(binding.store_path), _marker()
    layout.control_root.mkdir()
    if kind in {"limit", "large"}:
        size = 16 * 1024 + (kind == "large")
        layout.authority_marker.write_bytes(marker + b" " * (size - len(marker)))
    elif kind == "directory":
        layout.authority_marker.mkdir()
    else:
        os.mkfifo(layout.authority_marker)
    error = RefreshRequiredError if kind == "limit" else ReplicaControlReadError
    with pytest.raises(error), _locked(binding):
        pass


@pytest.mark.parametrize(
    "operation", ["metadata", "open", "open-replace", "read-replace", "read", "flags"]
)
def test_control_io_and_replacement_failures_are_typed(tmp_path, monkeypatch, operation):
    path = tmp_path / "control.json"
    path.write_bytes(b"{}")
    if operation == "flags":
        for name in ("O_NONBLOCK", "O_NOFOLLOW"):
            assert control._READ_FLAGS & getattr(os, name, 0) == getattr(os, name, 0)
        return
    if operation == "metadata":
        monkeypatch.setattr(control.os, "lstat", lambda _: (_ for _ in ()).throw(OSError()))
    elif operation == "open":
        monkeypatch.setattr(control.os, "open", lambda *_: (_ for _ in ()).throw(OSError()))
    elif "replace" in operation:
        original, calls = os.fstat, 0

        def fstat(fd: int):
            nonlocal calls
            calls += 1
            values, attributes = original(fd).__reduce__()[1]
            values = list(values)
            values[1] += calls == (1 if operation == "open-replace" else 2)
            return os.stat_result(values, attributes)

        monkeypatch.setattr(control.os, "fstat", fstat)
    else:
        monkeypatch.setattr(control.os, "read", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ReplicaControlReadError) as raised:
        control._read_optional_file(path)
    assert raised.value.code == "generation_control_unavailable"
