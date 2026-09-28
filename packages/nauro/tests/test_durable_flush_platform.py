from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import time

import pytest

from nauro.store import _platform_durability as durability
from nauro.store import generation_refresh_io as durable
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.sync import generation_refresh as refresh
from nauro.sync.reference_credentials import CredentialStore
from tests.test_generation_installation import USER_ID
from tests.test_generation_refresh import _bootstrap
from tests.test_generation_refresh import replica as replica

NATIVE = os.name == "nt"
REAL_CALLS = {"CreateFileW", "FlushFileBuffers", "CloseHandle", "MoveFileExW"}


class _Function:
    def __init__(self, kernel: FakeKernel32, name: str) -> None:
        self.kernel, self.name = kernel, name

    def __call__(self, *args):
        return self.kernel.invoke(self, args)


class FakeKernel32:
    """Records kernel32 calls; delegates file calls to the real library on Windows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, object]] = []
        self.fail: dict[str, int] = {}
        self.file_system, self.error, self.handles = "NTFS", 0, {}

    def __getattr__(self, name: str) -> _Function:
        return _Function(self, name)

    def invoke(self, function: _Function, args: tuple) -> object:
        name, self.error = function.name, self.fail.get(function.name, 0)
        result = None if name == "CreateFileW" else 0
        if not self.error:
            result = self._succeed(function, args)
        self.calls.append((name, args, result))
        return result

    def _succeed(self, function: _Function, args: tuple) -> object:
        if NATIVE and function.name in REAL_CALLS:
            real = getattr(ctypes.WinDLL("kernel32", use_last_error=True), function.name)
            real.argtypes, real.restype = function.argtypes, function.restype
            result = real(*args)
            self.error = ctypes.get_last_error()
            return result
        if function.name == "CreateFileW":
            self.handles[1000 + len(self.handles)] = args[0]
            return 999 + len(self.handles)
        if function.name == "MoveFileExW":
            (os.replace if args[2] & 1 else os.rename)(args[0], args[1])
        elif function.name == "GetVolumePathNameW":
            args[1].value = "C:\\"
        elif function.name == "GetVolumeInformationW":
            args[6].value = self.file_system
        return 1

    def named(self, name: str) -> list[tuple]:
        return [(args, result) for called, args, result in self.calls if called == name]


@pytest.fixture
def kernel(monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(durability, "WINDOWS", True)
    monkeypatch.setattr(durability, "_kernel32", lambda: fake)
    monkeypatch.setattr(durability, "_last_error", lambda reset=False: 0 if reset else fake.error)
    if not NATIVE:
        opened: dict[int, int] = {}

        def descriptor(handle):
            fd = os.open(fake.handles[handle], os.O_RDONLY)
            opened[fd] = handle
            return fd

        monkeypatch.setattr(durability, "_descriptor", descriptor)
        monkeypatch.setattr(durability, "_os_handle", lambda fd: opened.get(fd, -fd))
    return fake


def _paths(tmp_path):
    directory = tmp_path / "evidence"
    directory.mkdir()
    return durable.RefreshPaths(tmp_path, tmp_path), directory


def test_windows_branch_is_selected_by_os_name():
    assert durability.WINDOWS is (os.name == "nt")


def test_platform_barriers_after_write_and_rename(tmp_path):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    durable.durable_replace(paths, target, b"first")
    durable.durable_replace(paths, target, b"second")
    with open(target, "rb") as reader:
        durable.sync_file(paths, target)
        assert reader.read() == b"second"
    durable.sync_parents(paths, directory)
    CredentialStore(directory / "credentials.json", "binding", USER_ID).sync_directory()
    durable.probe_durability(directory)
    assert sorted(p.name for p in directory.iterdir()) == ["record.json"]


def test_windows_directory_barrier_opens_nothing(tmp_path, monkeypatch, kernel):
    paths, directory = _paths(tmp_path)
    opens = []
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: opens.append(args))
    durable.sync_directory(paths, directory)
    durable.sync_parents(paths, directory)
    CredentialStore(directory / "credentials.json", "binding", USER_ID).sync_directory()
    assert (opens, kernel.calls) == ([], [])


def test_windows_file_flush_uses_its_own_write_handle(tmp_path, kernel):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    target.write_bytes(b"bytes")
    durable.sync_file(paths, target)
    assert [name for name, _, _ in kernel.calls] == ["CreateFileW", "FlushFileBuffers"]
    [(args, handle)] = kernel.named("CreateFileW")
    assert args == (
        str(target),
        durability.GENERIC_WRITE | durability.FILE_READ_ATTRIBUTES,
        0x7,
        None,
        durability.OPEN_EXISTING,
        durability.FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    assert kernel.named("FlushFileBuffers") == [((handle,), 1)]


def test_windows_refused_flush_fails_closed(tmp_path, kernel):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    target.write_bytes(b"bytes")
    kernel.fail["FlushFileBuffers"] = 1117
    with pytest.raises(durability.DurabilityUnavailableError, match="Windows error 1117") as raised:
        durable.sync_file(paths, target)
    assert "record.json" in str(raised.value)


@pytest.mark.parametrize(
    "error,raised,message",
    [
        (5, durability.DurabilityUnavailableError, "read-only"),
        (32, durability.DurabilityUnavailableError, "another process has it open"),
        (1117, durability.DurabilityUnavailableError, "Windows error 1117"),
        (2, FileNotFoundError, "No such file"),
    ],
)
def test_windows_refused_open_names_path_and_reason(tmp_path, kernel, error, raised, message):
    paths, directory = _paths(tmp_path)
    kernel.fail["CreateFileW"] = error
    with pytest.raises(raised, match=message) as caught:
        durable.sync_file(paths, directory / "record.json")
    assert "record.json" in str(caught.value)
    assert kernel.named("FlushFileBuffers") == []


def test_windows_renames_write_through(tmp_path, kernel):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    durable.durable_replace(paths, target, b"first")
    durable.durable_replace(paths, target, b"second")
    durability.durable_rename(directory, tmp_path / "moved", replace=False)
    flags = [args[2] for args, _ in kernel.named("MoveFileExW")]
    assert flags == [0x1 | 0x8, 0x1 | 0x8, 0x8]
    assert (tmp_path / "moved" / "record.json").read_bytes() == b"second"


def test_windows_refused_rename_leaves_destination(tmp_path, kernel):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    durable.durable_replace(paths, target, b"first")
    kernel.fail["MoveFileExW"] = 5
    with pytest.raises(durability.DurableRenameError, match="read-only") as raised:
        durable.durable_replace(paths, target, b"second")
    assert isinstance(raised.value, OSError)
    assert target.read_bytes() == b"first"
    assert sorted(p.name for p in directory.iterdir()) == ["record.json"]


def test_windows_archive_publishes_by_write_through_rename(tmp_path, monkeypatch, kernel):
    actor = tmp_path / "actor"
    actor.mkdir()
    paths, raw = durable.RefreshPaths(tmp_path, actor), b'{"intent":1}'
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(os, "link", lambda *args: pytest.fail("Windows archive used a link."))
    kernel.fail["MoveFileExW"] = 5
    with pytest.raises(durability.DurableRenameError):
        durable.preserve_predecessor(paths, digest, raw)
    assert list(paths.history.iterdir()) == []
    del kernel.fail["MoveFileExW"]
    durable.preserve_predecessor(paths, digest, raw)
    archive = paths.history / f"{digest}.json"
    assert [(args[1], args[2]) for args, _ in kernel.named("MoveFileExW")] == [
        (str(archive), 0x8),
        (str(archive), 0x8),
    ]
    assert archive.read_bytes() == raw
    assert [p.name for p in paths.history.iterdir()] == [archive.name]


@pytest.mark.parametrize("name", ["NTFS", "ReFS", "FAT32", "exFAT", ""])
def test_windows_probe_admits_only_journaled_file_systems(tmp_path, kernel, name):
    directory = _paths(tmp_path)[1]
    kernel.file_system = name
    if name in {"NTFS", "ReFS"}:
        durable.probe_durability(directory)
        assert [args[2] for args, _ in kernel.named("MoveFileExW")] == [0x8]
    else:
        with pytest.raises(durability.DurabilityUnavailableError, match="need NTFS or ReFS"):
            durable.probe_durability(directory)
        assert kernel.named("CreateFileW") == []
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("failing", ["GetVolumeInformationW", "FlushFileBuffers", "MoveFileExW"])
def test_windows_probe_refusal_is_typed_and_leaves_nothing(tmp_path, kernel, failing):
    directory = _paths(tmp_path)[1]
    kernel.fail[failing] = 87
    with pytest.raises(durability.DurabilityUnavailableError, match="evidence"):
        durable.probe_durability(directory)
    assert list(directory.iterdir()) == []


@pytest.mark.skipif(NATIVE, reason="the POSIX branch opens directories through the C runtime")
def test_posix_branch_keeps_fsync_link_and_os_rename(tmp_path, monkeypatch):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    target.write_bytes(b"bytes")
    calls = []
    for name in ("replace", "rename", "link"):
        original = getattr(os, name)
        monkeypatch.setattr(os, name, lambda *a, _n=name, _o=original: calls.append(_n) or _o(*a))
    durability.durable_rename(target, directory / "moved.json")
    durability.durable_rename(directory / "moved.json", target, replace=False)
    raw = b"archive"
    durable.preserve_predecessor(paths, hashlib.sha256(raw).hexdigest(), raw)
    assert calls == ["replace", "rename", "link"]

    def refuse(descriptor):
        raise OSError("fsync refused")

    monkeypatch.setattr(os, "fsync", refuse)
    for call in (
        lambda: durable.sync_directory(paths, directory),
        lambda: durable.sync_file(paths, target),
    ):
        with pytest.raises(OSError, match="fsync refused"):
            call()
    with pytest.raises(durability.DurabilityUnavailableError, match="cannot confirm") as raised:
        durable.probe_durability(directory)
    assert isinstance(raised.value.__cause__, OSError)


def test_refused_flush_stops_refresh_before_intent_or_control_mutation(replica, kernel):
    binding, _ = replica
    paths = durable.refresh_paths(binding, USER_ID)
    before = paths.pointer.read_bytes(), paths.carrier.read_bytes()
    prepared = _bootstrap(binding)
    kernel.fail["FlushFileBuffers"] = 1
    with pytest.raises(durability.DurabilityUnavailableError):
        refresh.commit_generation_refresh(prepared)
    assert (paths.pointer.read_bytes(), paths.carrier.read_bytes()) == before
    assert not paths.intent.exists()


def test_windows_control_publication_renames_through(monkeypatch, kernel):
    from nauro.store import generation_installation as installation
    from tests.test_generation_installation import _projection

    base = _projection()
    base.target.binding.store_path.mkdir(parents=True)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    installation.publish_generation_control(installation.install_generation_root(base))
    control = durable.refresh_paths(base.target.binding, USER_ID)
    renamed = {args[1]: args[2] for args, _ in kernel.named("MoveFileExW")}
    for path in (control.carrier, control.pointer, control.marker):
        assert renamed[str(path)] == 0x1 | 0x8


def test_windows_saved_plan_renames_through(tmp_path, monkeypatch, kernel):
    from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
    from nauro.sync import migration_admission as migration
    from tests.test_generation_migration_plan import _assessment

    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    plan = prepare_legacy_migration_plan(_assessment(tmp_path)[1])
    record = migration.save_migration_assessment(plan)
    renamed = {args[1]: args[2] for args, _ in kernel.named("MoveFileExW")}
    assert renamed[str(migration._plan_path(record))] == 0x1 | 0x8


def test_windows_rename_refuses_unjournaled_volume_before_moving(tmp_path, kernel):
    paths, directory = _paths(tmp_path)
    (directory / "a").write_bytes(b"a")
    kernel.file_system = "FAT32"
    with pytest.raises(durability.DurabilityUnavailableError, match="FAT32"):
        durability.durable_rename(directory / "a", directory / "b")
    assert kernel.named("MoveFileExW") == []
    assert [name for name, _, _ in kernel.calls] == ["GetVolumePathNameW", "GetVolumeInformationW"]
    assert kernel.named("GetVolumePathNameW")[0][0][0] == str(directory)


def test_windows_directory_barrier_refuses_missing_or_file(tmp_path, kernel):
    paths, directory = _paths(tmp_path)
    (directory / "file").write_bytes(b"x")
    with pytest.raises(FileNotFoundError):
        durable.sync_directory(paths, directory / "missing")
    with pytest.raises(NotADirectoryError):
        durable.sync_directory(paths, directory / "file")


def test_windows_probe_refuses_unknown_volume(tmp_path, kernel):
    directory = _paths(tmp_path)[1]
    kernel.fail["GetVolumePathNameW"] = 3
    with pytest.raises(durability.DurabilityUnavailableError, match="Cannot find the volume"):
        durable.probe_durability(directory)
    assert kernel.named("GetVolumeInformationW") == []


def test_probe_cleanup_attempts_every_file_and_is_typed(tmp_path, monkeypatch):
    directory = _paths(tmp_path)[1]
    unlink, refused = type(directory).unlink, []

    def refuse_first(path, missing_ok=False):
        if path.name.startswith(".durability-probe") and not refused:
            refused.append(path)
            raise PermissionError("in use")
        return unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(type(directory), "unlink", refuse_first)
    with pytest.raises(durability.DurabilityUnavailableError, match="Cannot remove") as raised:
        durable.probe_durability(directory)
    assert str(refused[0]) in str(raised.value)
    assert [p for p in directory.iterdir() if p != refused[0]] == []


def test_probe_cleans_up_when_validation_escapes(tmp_path, monkeypatch):
    from nauro.store.generation_refresh_state import GenerationRefreshEvidenceError

    directory = _paths(tmp_path)[1]

    def invalid(*args):
        raise GenerationRefreshEvidenceError("Refresh file identity changed.")

    monkeypatch.setattr(durable, "sync_file", invalid)
    with pytest.raises(GenerationRefreshEvidenceError):
        durable.probe_durability(directory)
    assert list(directory.iterdir()) == []


def test_probe_never_touches_an_occupied_name(tmp_path, monkeypatch):
    directory = _paths(tmp_path)[1]
    monkeypatch.setattr(durable.secrets, "token_hex", lambda n: "0" * (2 * n))
    occupied = directory / f".durability-probe-{'0' * 16}"
    occupied.write_bytes(b"keep")
    with pytest.raises(durability.DurabilityUnavailableError, match="is taken"):
        durable.probe_durability(directory)
    assert list(directory.iterdir()) == [occupied]
    assert occupied.read_bytes() == b"keep"


def test_probe_closes_descriptor_when_write_fails(tmp_path, monkeypatch):
    directory = _paths(tmp_path)[1]
    opened, closed, real_open, real_close = [], [], durable._open_random_tmp, os.close

    def open_tmp(path, mode):
        opened.append(real_open(path, mode))
        return opened[-1]

    def refuse(*args, **kwargs):
        raise OSError("no write")

    monkeypatch.setattr(durable, "_open_random_tmp", open_tmp)
    monkeypatch.setattr(os, "write", refuse)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd) or real_close(fd))
    with pytest.raises(durability.DurabilityUnavailableError, match="cannot confirm"):
        durable.probe_durability(directory)
    assert closed == [opened[0][0]]
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("name", ["authorization-view.json", "pointer.json", "authority.json"])
def test_control_publication_fails_closed_on_durability_error(monkeypatch, name):
    from nauro.store import generation_installation as installation
    from tests.test_generation_installation import _projection

    real = durability.durable_rename

    def visible_but_not_durable(source, destination, **kwargs):
        real(source, destination, **kwargs)
        if destination.name == name:
            raise durability.DurableRenameError(f"Cannot move {source} durably.")

    base = _projection()
    base.target.binding.store_path.mkdir(parents=True)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    installed = installation.install_generation_root(base)
    monkeypatch.setattr(durability, "durable_rename", visible_but_not_durable)
    with pytest.raises(GenerationAuthorityError):
        installation.publish_generation_control(installed)


def _link_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.parametrize("branch", ["platform", "windows"])
@pytest.mark.parametrize("validated", [True, False])
def test_sync_file_refuses_a_linked_file(tmp_path, monkeypatch, request, branch, validated):
    paths, directory = _paths(tmp_path)
    (directory / "real.json").write_bytes(b"x")
    link = directory / "link.json"
    _link_or_skip(link, directory / "real.json")
    kernel = request.getfixturevalue("kernel") if branch == "windows" else None
    if not validated:
        monkeypatch.setattr(durable, "_validate_managed_path", lambda *args: None)
    expected = (
        (GenerationAuthorityError, OSError) if branch == "platform" else GenerationAuthorityError
    )
    with pytest.raises(expected):
        durable.sync_file(paths, link)
    if kernel is not None:
        assert kernel.named("FlushFileBuffers") == []


def test_windows_flush_failure_closes_descriptor(tmp_path, monkeypatch, kernel):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    target.write_bytes(b"bytes")
    opened, closed, real_open, real_close = [], [], durability.open_writable, os.close
    monkeypatch.setattr(
        durability, "open_writable", lambda p: opened.append(real_open(p)) or opened[-1]
    )
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd) or real_close(fd))
    kernel.fail["FlushFileBuffers"] = 1117
    with pytest.raises(durability.DurabilityUnavailableError):
        durable.sync_file(paths, target)
    assert closed == opened


def test_windows_failed_descriptor_closes_the_handle(tmp_path, monkeypatch, kernel):
    paths, directory = _paths(tmp_path)
    target = directory / "record.json"
    target.write_bytes(b"bytes")

    def refuse(handle):
        raise OSError("no descriptor")

    monkeypatch.setattr(durability, "_descriptor", refuse)
    with pytest.raises(OSError, match="no descriptor"):
        durable.sync_file(paths, target)
    [(_, handle)] = kernel.named("CreateFileW")
    assert kernel.named("CloseHandle") == [((handle,), 1)]
    assert kernel.named("FlushFileBuffers") == []


def test_flush_cost_is_printed_for_the_platform_job(tmp_path, capsys):
    paths, files = durable.RefreshPaths(tmp_path, tmp_path), []
    for index in range(600):
        files.append(tmp_path / f"artifact-{index:03}.md")
        files[-1].write_bytes(b"# Artifact\n" * 16)
    started = time.perf_counter()
    for path in files:
        durable.sync_file(paths, path)
    elapsed = time.perf_counter() - started
    with capsys.disabled():
        print(
            f"\nFLUSH-COST platform={sys.platform} files={len(files)} "
            f"total_s={elapsed:.3f} per_file_ms={elapsed * 1000 / len(files):.3f}"
        )
