"""Legacy push enumerates the Store only through the sync path-admission walker."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
)
from nauro.sync import cloud_projects, remote
from nauro.sync import merge as merge_module
from nauro.sync import push as push_module
from nauro.sync._path_diagnostics import _StoreRootPreparationError
from nauro.sync.hooks import push_after_write
from nauro.sync.lock import SYNC_LOCK_FILE
from nauro.sync.push import (
    OversizedBrief,
    PushReport,
    SkippedUnsafePath,
    plan_push,
    push_changed_files,
    push_store_to_cloud,
)
from nauro.sync.state import FileState, SyncState, compute_sha256, load_state, save_state
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import seed_auth_config
from tests.test_sync.conftest import CLOUD_PID, _scaffolded_cloud_project, track

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename semantics")
ROOT_TEXT = "The Store root is unavailable."
OUTSIDE_TEXT = "The path resolves outside the Store root."
PARENT_TEXT = "A path component normalizes to parent."
METADATA_TEXT = "Path metadata is unavailable."
OBSERVED_TEXT = "The path changed after it was observed."
NOTE_KEY = os.path.join("context", "note.md")
runner = CliRunner()


def _plain_store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    (store / "context").mkdir(parents=True)
    (store / "context" / "note.md").write_text("note\n")
    return store


def _cloud_store(tmp_path: Path, name: str = "pushadm") -> Path:
    store = _scaffolded_cloud_project(name, tmp_path, project_id=CLOUD_PID)
    seed_auth_config(variant="sync")
    return store


def _track_all(store: Path) -> None:
    for path in store.rglob("*"):
        if path.is_file() and not path.is_symlink():
            track(store, str(path.relative_to(store)))


def _presign_post(url, **kwargs):
    operations = kwargs["json"]["operations"]
    return httpx.Response(
        200,
        json={
            "urls": [
                {
                    "verb": op["verb"],
                    "path": op["path"],
                    "url": f"https://s3.example/PUT/{op['path']}",
                    "expires_at": "2026-05-29T13:00:00Z",
                }
                for op in operations
            ]
        },
        request=httpx.Request("POST", url),
    )


def _ok_put():
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {"ETag": '"e_pushed"'}
    return response


def _run_push(store: Path) -> tuple[PushReport, list[str]]:
    with (
        patch.object(remote.httpx.Client, "post", side_effect=_presign_post) as post,
        patch.object(remote.httpx.Client, "put", return_value=_ok_put()),
    ):
        report = push_changed_files(CLOUD_PID, store)
    put_paths = [
        op["path"] for call in post.call_args_list for op in call.kwargs["json"]["operations"]
    ]
    return report, put_paths


def _is_reserved(path: Path) -> bool:
    for part in path.parts:
        folded = part.split(":", 1)[0].strip(" ").rstrip(" .").casefold()
        if folded in {_REPLICA_CONTROL_ROOT_NAME, _REPLICA_CONTROL_LOCK_NAME}:
            return True
    return False


def _guard_access(monkeypatch, forbidden) -> None:
    real_stat, real_read, real_sha = Path.stat, Path.read_bytes, push_module.compute_sha256

    def check(path) -> None:
        if forbidden(Path(path)):
            pytest.fail(f"accessed {path}")

    def stat(self, *args, **kwargs):
        check(self)
        return real_stat(self, *args, **kwargs)

    def read_bytes(self):
        check(self)
        return real_read(self)

    def sha(path):
        check(path)
        return real_sha(path)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(push_module, "compute_sha256", sha)


# --- plan table ---


@pytest.mark.parametrize(
    "reserved",
    [
        _REPLICA_CONTROL_ROOT_NAME,
        ".Replica",
        f" {_REPLICA_CONTROL_ROOT_NAME}",
        pytest.param(f"{_REPLICA_CONTROL_ROOT_NAME}.", marks=POSIX_ONLY),
        pytest.param(f"{_REPLICA_CONTROL_ROOT_NAME} ", marks=POSIX_ONLY),
        pytest.param(f"{_REPLICA_CONTROL_ROOT_NAME}:stream", marks=POSIX_ONLY),
    ],
)
def test_reserved_root_aliases_never_become_candidates(tmp_path: Path, reserved: str) -> None:
    store = _plain_store(tmp_path)
    (store / reserved / "pointer").mkdir(parents=True)
    (store / reserved / "authority.json").write_text("{}")
    (store / reserved / "pointer" / "actor.json").write_text("{}")
    plan = plan_push(store, SyncState())
    assert [c.relative_path for c in plan.candidates] == [NOTE_KEY]
    assert plan.unsafe == ()


@pytest.mark.parametrize(
    "reserved",
    [
        _REPLICA_CONTROL_LOCK_NAME,
        ".Replica-Control.LOCK",
        f" {_REPLICA_CONTROL_LOCK_NAME}",
        pytest.param(f"{_REPLICA_CONTROL_LOCK_NAME}.", marks=POSIX_ONLY),
        pytest.param(f"{_REPLICA_CONTROL_LOCK_NAME}\\child", marks=POSIX_ONLY),
    ],
)
def test_reserved_lock_aliases_never_become_candidates(tmp_path: Path, reserved: str) -> None:
    store = _plain_store(tmp_path)
    (store / reserved).write_text("lock")
    plan = plan_push(store, SyncState())
    assert [c.relative_path for c in plan.candidates] == [NOTE_KEY]
    assert plan.unsafe == ()


def test_ordinary_keys_and_exclusions_are_unchanged(tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "context").mkdir(parents=True)
    (store / "stack.md").write_text("stack\n")
    (store / "context" / "new.md").write_text("new\n")
    (store / "context" / "draft.md.lock").write_text("lock\n")
    (store / "journal").mkdir()
    (store / "journal" / "events.jsonl").write_text("{}\n")
    (store / ".pull-spool-dead").mkdir()
    (store / ".pull-spool-dead" / "000000").write_text("scratch\n")
    (store / ".conflict-backup").mkdir()
    (store / ".conflict-backup" / "loser.md").write_text("backup\n")
    (store / "__pycache__").mkdir()
    (store / "__pycache__" / "x.pyc").write_text("pyc\n")
    (store / "nauro-graph.html").write_text("generated\n")
    (store / ".sync-state.json").write_text("{}\n")
    (store / "deleted.md").write_text("gone\n")
    state = SyncState()
    state.files["deleted.md"] = FileState(
        local_sha256=compute_sha256(store / "deleted.md"),
        remote_etag='"synced"',
        last_sync="2026-05-16T00:00:00Z",
    )
    (store / "deleted.md").unlink()
    plan = plan_push(store, state)
    assert sorted(c.relative_path for c in plan.candidates) == sorted(
        ["stack.md", os.path.join("context", "new.md")]
    )
    assert plan.oversized_briefs == ()
    assert plan.unsafe == ()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="oversized detection keys on the POSIX 'context/' prefix",
)
def test_oversized_brief_is_reported_and_never_a_candidate(tmp_path: Path) -> None:
    store = _plain_store(tmp_path)
    size = push_module.MAX_BRIEF_BYTES + 1
    (store / "context" / "big.md").write_text("x" * size)
    plan = plan_push(store, SyncState())
    assert plan.oversized_briefs == (OversizedBrief(os.path.join("context", "big.md"), size),)
    assert [c.relative_path for c in plan.candidates] == [NOTE_KEY]
    assert plan.unsafe == ()


def test_plan_push_never_uses_rglob(tmp_path: Path, monkeypatch) -> None:
    store = _plain_store(tmp_path)
    monkeypatch.setattr(Path, "rglob", lambda *args, **kwargs: pytest.fail("rglob"))
    assert [c.relative_path for c in plan_push(store, SyncState()).candidates] == [NOTE_KEY]


# --- access-order table ---


def test_push_never_reads_reserved_paths(tmp_path: Path, monkeypatch) -> None:
    store = _cloud_store(tmp_path)
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "authority.json").write_text("{}")
    (store / _REPLICA_CONTROL_LOCK_NAME).write_text("lock")
    (store / "stack.md").write_text("changed\n")
    _guard_access(monkeypatch, _is_reserved)
    captured: list[list[dict]] = []

    def fake_presign(project_id, operations, **_kwargs):
        captured.append(operations)
        return []

    with patch("nauro.sync.remote.request_presigned_urls", side_effect=fake_presign):
        report = push_changed_files(CLOUD_PID, store)

    paths = [op["path"] for ops in captured for op in ops]
    assert "stack.md" in paths
    assert not any(_is_reserved(Path(path)) for path in paths)
    assert report.planned == tuple(paths)


@POSIX_ONLY
def test_push_never_reads_control_or_outside_link_targets(tmp_path: Path, monkeypatch) -> None:
    store = _cloud_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret\n")
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "authority.json").write_text("{}")
    (store / "alias").symlink_to(store / _REPLICA_CONTROL_ROOT_NAME / "authority.json")
    (store / "evil").symlink_to(outside / "secret")
    _track_all(store)
    _guard_access(
        monkeypatch,
        lambda path: _is_reserved(path) or path == outside or outside in path.parents,
    )

    with patch("nauro.sync.remote.request_presigned_urls", side_effect=AssertionError):
        report = push_changed_files(CLOUD_PID, store)

    assert report == PushReport()


# --- link table ---


@POSIX_ONLY
def test_link_entries_follow_admission(tmp_path: Path) -> None:
    store = _plain_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret\n")
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "authority.json").write_text("{}")
    (store / _REPLICA_CONTROL_LOCK_NAME).write_text("lock")
    (store / "file-link").symlink_to("context/note.md")
    (store / "root-link").symlink_to(f"{_REPLICA_CONTROL_ROOT_NAME}/authority.json")
    (store / "lock-link").symlink_to(_REPLICA_CONTROL_LOCK_NAME)
    (store / "evil").symlink_to(outside / "secret")
    (store / "dir-link").symlink_to("context", target_is_directory=True)
    (store / "dangling").symlink_to("missing")
    plan = plan_push(store, SyncState())
    assert sorted(c.relative_path for c in plan.candidates) == [NOTE_KEY, "file-link"]
    assert len(plan.unsafe) == 2
    assert set(plan.unsafe) == {
        SkippedUnsafePath("dangling", OBSERVED_TEXT),
        SkippedUnsafePath("evil", OUTSIDE_TEXT),
    }


# --- unsafe table ---


@POSIX_ONLY
def test_unsafe_directory_is_reported_once_and_never_pushed(tmp_path: Path, capsys) -> None:
    store = _cloud_store(tmp_path)
    _track_all(store)
    hostile = store / " .. "
    hostile.mkdir()
    (hostile / "hidden").write_text("hidden\n")
    (store / "stack.md").write_text("changed\n")

    report, put_paths = _run_push(store)

    assert report.is_complete and report.planned == ("stack.md",)
    assert put_paths == ["stack.md"]
    warnings = [line for line in capsys.readouterr().err.splitlines() if "not pushed" in line]
    assert warnings == [f"  Warning: \\x20..\\x20 was not pushed: {PARENT_TEXT}"]


def test_unscannable_directory_is_reported_once_and_never_pushed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = _cloud_store(tmp_path)
    _track_all(store)
    blocked = store / "blocked"
    blocked.mkdir()
    (blocked / "hidden").write_text("hidden\n")
    real_scandir = merge_module.os.scandir

    def guarded_scandir(path):
        if Path(path) == blocked:
            raise OSError("sentinel filesystem detail")
        return real_scandir(path)

    monkeypatch.setattr(merge_module.os, "scandir", guarded_scandir)

    report, put_paths = _run_push(store)

    assert report == PushReport()
    assert put_paths == []
    err = capsys.readouterr().err
    assert err.splitlines() == [f"  Warning: blocked was not pushed: {METADATA_TEXT}"]
    assert "sentinel" not in err


def test_unscannable_store_root_is_reported_as_the_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = _cloud_store(tmp_path)
    _track_all(store)
    canonical = store.resolve()
    real_scandir = merge_module.os.scandir

    def guarded_scandir(path):
        if Path(path) == canonical:
            raise OSError("sentinel filesystem detail")
        return real_scandir(path)

    monkeypatch.setattr(merge_module.os, "scandir", guarded_scandir)
    plan = plan_push(store, load_state(store))
    assert plan.candidates == ()
    assert plan.unsafe == (SkippedUnsafePath("(Store root)", METADATA_TEXT),)

    report, put_paths = _run_push(store)

    assert report == PushReport()
    assert put_paths == []
    err = capsys.readouterr().err
    assert err.splitlines() == [f"  Warning: (Store root) was not pushed: {METADATA_TEXT}"]
    assert "sentinel" not in err


# --- root table ---

BAD_ROOTS = ["file", "missing"]


def _bad_root(tmp_path: Path, condition: str) -> Path:
    root = tmp_path / "bad-store"
    if condition == "file":
        root.write_text("not a directory")
    return root


def _assert_root_untouched(root: Path, condition: str) -> None:
    if condition == "missing":
        assert not root.exists()
    else:
        assert root.is_file()
    assert not (root / SYNC_LOCK_FILE).is_file()


def _assert_no_leak(text: str, path: Path) -> None:
    assert str(path) not in text
    assert path.name not in text
    for marker in ("Errno", "No such file", "Not a directory", "File exists"):
        assert marker not in text


@pytest.mark.parametrize("condition", BAD_ROOTS)
def test_plan_push_raises_fixed_error_for_bad_root(tmp_path: Path, condition: str) -> None:
    root = _bad_root(tmp_path, condition)
    with pytest.raises(_StoreRootPreparationError) as raised:
        plan_push(root, SyncState())
    assert str(raised.value) == ROOT_TEXT
    _assert_root_untouched(root, condition)


@pytest.mark.parametrize("condition", BAD_ROOTS)
def test_push_changed_files_refuses_bad_root_before_the_lock(
    tmp_path: Path, condition: str
) -> None:
    _cloud_store(tmp_path)
    root = _bad_root(tmp_path, condition)
    with (
        patch("nauro.sync.remote.request_presigned_urls", side_effect=AssertionError),
        pytest.raises(_StoreRootPreparationError) as raised,
    ):
        push_changed_files(CLOUD_PID, root)
    assert str(raised.value) == ROOT_TEXT
    _assert_root_untouched(root, condition)


@pytest.mark.parametrize("condition", BAD_ROOTS)
def test_push_store_to_cloud_reports_store_root(tmp_path: Path, capsys, condition: str) -> None:
    _cloud_store(tmp_path)
    root = _bad_root(tmp_path, condition)
    with patch("nauro.sync.remote.request_presigned_urls", side_effect=AssertionError):
        report = push_store_to_cloud(CLOUD_PID, root)
    assert report == PushReport(failed=("store root",))
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"  Warning: cloud push failed ({ROOT_TEXT})"]
    _assert_no_leak(captured.out + captured.err, root)
    _assert_root_untouched(root, condition)


@pytest.mark.parametrize("condition", BAD_ROOTS)
def test_push_after_write_reports_store_root_without_raising(
    tmp_path: Path, caplog, capsys, condition: str
) -> None:
    _cloud_store(tmp_path)
    root = _bad_root(tmp_path, condition)
    with caplog.at_level(logging.WARNING, logger="nauro.sync"):
        report = push_after_write(CLOUD_PID, root)
    assert report == PushReport(failed=("store root",))
    assert f"sync push: {ROOT_TEXT}" in caplog.text
    captured = capsys.readouterr()
    _assert_no_leak(caplog.text + captured.out + captured.err, root)
    _assert_root_untouched(root, condition)


@pytest.mark.parametrize("condition", BAD_ROOTS)
def test_link_cloud_exits_without_traceback_on_bad_store_root(
    tmp_path: Path, monkeypatch, condition: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    seed_auth_config()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")
    assert runner.invoke(app, ["init", "linkproj"]).exit_code == 0
    local_id, _entry = registry.find_projects_by_name_v2("linkproj")[0]
    store = registry.get_store_path_v2(local_id)
    shutil.rmtree(store)
    if condition == "file":
        store.write_text("not a directory")

    with (
        patch.object(cloud_projects.httpx, "request", side_effect=AssertionError),
        patch.object(remote.httpx.Client, "post", side_effect=AssertionError),
    ):
        result = runner.invoke(app, ["link", "--cloud"])

    # link resolves the registered project before it reaches the shared push,
    # so a bad Store root is refused there and the promotion never starts.
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "nauro reconnect" in result.output
    _assert_no_leak(result.output, store)
    assert registry.get_project_v2(local_id) is not None
    assert registry.get_project_v2(CLOUD_PID) is None


# --- state table ---


def test_reserved_state_entries_are_left_untouched(tmp_path: Path) -> None:
    store = _cloud_store(tmp_path)
    _track_all(store)
    reserved_key = os.path.join(_REPLICA_CONTROL_ROOT_NAME, "authority.json")
    seeded = FileState(
        local_sha256="0" * 64, remote_etag='"stale"', last_sync="2026-01-01T00:00:00Z"
    )
    state = load_state(store)
    state.files[reserved_key] = seeded
    save_state(store, state)
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "authority.json").write_text("{}")
    (store / "context").mkdir(exist_ok=True)
    (store / "context" / "new.md").write_text("new\n")

    report, put_paths = _run_push(store)

    new_key = os.path.join("context", "new.md")
    assert report.is_complete and report.verified == (new_key,)
    assert put_paths == [new_key]
    after = load_state(store)
    assert after.files[reserved_key] == seeded
    assert after.files[new_key].remote_etag == '"e_pushed"'


# --- status table ---


def _status_store(tmp_path: Path, monkeypatch) -> Path:
    _pid, store = register_project_v2("statusproj", [tmp_path])
    scaffold_project_store("statusproj", store)
    save_config({"auth": {"sub": "auth0|test", "access_token": "tok", "refresh_token": "refresh"}})
    monkeypatch.chdir(tmp_path)
    return store


def test_status_hides_reserved_and_lists_unscannable(tmp_path: Path, monkeypatch) -> None:
    store = _status_store(tmp_path, monkeypatch)
    _track_all(store)
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "authority.json").write_text("{}")
    (store / _REPLICA_CONTROL_LOCK_NAME).write_text("lock")
    blocked = store / "blocked"
    blocked.mkdir()
    real_scandir = merge_module.os.scandir

    def guarded_scandir(path):
        if Path(path) == blocked:
            raise OSError("sentinel filesystem detail")
        return real_scandir(path)

    monkeypatch.setattr(merge_module.os, "scandir", guarded_scandir)
    result = runner.invoke(app, ["sync", "--status"])

    assert result.exit_code == 0, result.output
    assert "Pending local changes: none" in result.output
    assert "Unsafe paths skipped: 1" in result.output
    assert f"    - blocked: {METADATA_TEXT}" in result.output
    assert "replica" not in result.output
    assert "sentinel" not in result.output


def test_status_lists_the_store_root_when_unscannable(tmp_path: Path, monkeypatch) -> None:
    store = _status_store(tmp_path, monkeypatch)
    canonical = store.resolve()
    real_scandir = merge_module.os.scandir

    def guarded_scandir(path):
        if Path(path) == canonical:
            raise OSError("sentinel filesystem detail")
        return real_scandir(path)

    monkeypatch.setattr(merge_module.os, "scandir", guarded_scandir)
    result = runner.invoke(app, ["sync", "--status"])

    assert result.exit_code == 0, result.output
    assert "  Unsafe paths skipped: 1" in result.output
    assert f"    - (Store root): {METADATA_TEXT}" in result.output
    assert "sentinel" not in result.output


@POSIX_ONLY
def test_status_caps_unsafe_lines_at_five(tmp_path: Path, monkeypatch) -> None:
    store = _status_store(tmp_path, monkeypatch)
    _track_all(store)
    for name in (" .. ", ".. ", ". ", " . ", "...", "  "):
        (store / name).mkdir()
    result = runner.invoke(app, ["sync", "--status"])

    assert result.exit_code == 0, result.output
    assert "Unsafe paths skipped: 6" in result.output
    listed = [line for line in result.output.splitlines() if line.startswith("    - ")]
    assert len(listed) == 5
    assert all(line.endswith(f": {PARENT_TEXT}") or ": A path component" in line for line in listed)
    assert "    ... and 1 more" in result.output
    assert " " not in "".join(line[6:].split(": ")[0] for line in listed)


def test_status_reports_fixed_unknown_line_for_bad_root(tmp_path: Path, monkeypatch) -> None:
    store = _status_store(tmp_path, monkeypatch)
    (store / ".conflict-backup").mkdir()
    (store / ".conflict-backup" / "loser.md").write_text("backup\n")
    monkeypatch.setattr(
        push_module, "_prepare_store_root", MagicMock(side_effect=_StoreRootPreparationError())
    )
    result = runner.invoke(app, ["sync", "--status"])

    assert result.exit_code == 0, result.output
    assert f"  Pending local changes: unknown ({ROOT_TEXT})" in result.output
    assert "Conflict backups: 1" in result.output
    assert str(store) not in result.output.split("Pending local changes", 1)[1]


# --- hook table ---


def test_hook_push_skips_reserved_and_unsafe_through_the_shared_plan(
    tmp_path: Path, monkeypatch
) -> None:
    store = _cloud_store(tmp_path)
    _track_all(store)
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "authority.json").write_text("{}")
    (store / "stack.md").write_text("changed\n")
    blocked = store / "blocked"
    blocked.mkdir()
    real_scandir = merge_module.os.scandir

    def guarded_scandir(path):
        if Path(path) == blocked:
            raise OSError("sentinel filesystem detail")
        return real_scandir(path)

    monkeypatch.setattr(merge_module.os, "scandir", guarded_scandir)
    with (
        patch.object(remote.httpx.Client, "post", side_effect=_presign_post) as post,
        patch.object(remote.httpx.Client, "put", return_value=_ok_put()),
    ):
        report = push_after_write(CLOUD_PID, store)

    assert report.is_complete and report.verified == ("stack.md",)
    ops = [op["path"] for call in post.call_args_list for op in call.kwargs["json"]["operations"]]
    assert ops == ["stack.md"]
