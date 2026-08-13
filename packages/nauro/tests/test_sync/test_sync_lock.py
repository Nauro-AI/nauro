"""Tests for the store-scoped sync lock.

The lock serializes pull and push per store with finite timeouts. The two
callers differ in what contention means: an explicit ``nauro sync`` fails loud,
the SessionStart hook skips its pull and logs, because session start must never
wait on another process.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from filelock import FileLock
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.store_lock import rmw_lock_path
from nauro.sync.hooks import pull_before_session, push_after_write
from nauro.sync.lock import (
    SYNC_LOCK_FILE,
    SyncLockTimeoutError,
    decision_lock,
    sync_lock,
)
from nauro.sync.merge import should_skip
from nauro.sync.pull import run_pull
from nauro.sync.push import PushReport, push_changed_files
from tests.test_sync.conftest import (
    CLOUD_PID,
    _ok,
    _RecordingReporter,
    _scaffolded_cloud_project,
    _seed_token,
)

runner = CliRunner()


@pytest.fixture()
def cloud_store(tmp_path):
    store = _scaffolded_cloud_project("lockproj", tmp_path, project_id=CLOUD_PID)
    _seed_token()
    return store


def _hold(store):
    """Acquire the store's sync lock the way another process would."""
    lock = FileLock(str(store / SYNC_LOCK_FILE))
    lock.acquire()
    return lock


class TestLockFile:
    def test_lock_artifact_is_never_synced(self):
        assert should_skip(SYNC_LOCK_FILE) is True

    def test_uncontended_acquisition_runs_the_block(self, cloud_store):
        with sync_lock(cloud_store, 0.1):
            assert (cloud_store / SYNC_LOCK_FILE).exists()

    def test_contended_acquisition_raises_with_the_store_named(self, cloud_store):
        lock = _hold(cloud_store)
        try:
            with pytest.raises(SyncLockTimeoutError) as excinfo:
                with sync_lock(cloud_store, 0.05):
                    pytest.fail("the lock should not have been granted")
        finally:
            lock.release()
        assert cloud_store.name in str(excinfo.value)

    def test_lock_is_released_when_the_block_raises(self, cloud_store):
        with pytest.raises(RuntimeError):
            with sync_lock(cloud_store, 0.1):
                raise RuntimeError("boom")
        with sync_lock(cloud_store, 0.1):
            pass


class TestContendedCallers:
    def test_run_pull_raises_before_touching_the_network(self, cloud_store):
        lock = _hold(cloud_store)
        try:
            with patch("nauro.sync.remote.httpx.Client.get") as get:
                with pytest.raises(SyncLockTimeoutError):
                    run_pull(CLOUD_PID, cloud_store, _RecordingReporter(), lock_timeout=0.05)
            assert get.call_count == 0
        finally:
            lock.release()

    def test_push_changed_files_raises(self, cloud_store):
        lock = _hold(cloud_store)
        try:
            with pytest.raises(SyncLockTimeoutError):
                push_changed_files(CLOUD_PID, cloud_store, lock_timeout=0.05)
        finally:
            lock.release()

    def test_session_start_hook_skips_and_logs(self, cloud_store, caplog):
        lock = _hold(cloud_store)
        try:
            with caplog.at_level(logging.INFO, logger="nauro.sync"):
                pulled = pull_before_session(CLOUD_PID, cloud_store)
        finally:
            lock.release()
        assert pulled == 0
        assert any("skipped" in record.getMessage() for record in caplog.records)

    def test_post_write_push_hook_skips_and_logs(self, cloud_store, caplog):
        lock = _hold(cloud_store)
        try:
            with caplog.at_level(logging.INFO, logger="nauro.sync"):
                pushed = push_after_write(CLOUD_PID, cloud_store)
        finally:
            lock.release()
        assert pushed == PushReport(failed=("sync lock",))
        assert any("skipped" in record.getMessage() for record in caplog.records)

    def test_cli_sync_fails_loud(self, cloud_store, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Same lock, same error, without spending the CLI's real bounded wait.
        monkeypatch.setattr(
            "nauro.sync.pull.sync_lock", lambda store_path, _timeout: sync_lock(store_path, 0.05)
        )
        lock = _hold(cloud_store)
        try:
            result = runner.invoke(app, ["sync"])
        finally:
            lock.release()
        assert result.exit_code == 1, result.output
        assert "another writer is busy" in result.output


class TestBoundedDecisionLock:
    """The store lock is not the only lock a pull takes.

    The write phase also takes the decision allocation lock, whose kernel-side
    acquisition waits forever by design. A sync must never inherit that wait:
    once the store lock is in hand, a suspended local writer would otherwise
    hold session start open indefinitely.
    """

    def _hold_decisions(self, store):
        lock = FileLock(str(rmw_lock_path(store, "decisions", is_directory=True)))
        lock.acquire()
        return lock

    def test_sync_decision_lock_is_bounded(self, cloud_store):
        lock = self._hold_decisions(cloud_store)
        try:
            with pytest.raises(SyncLockTimeoutError) as excinfo:
                with decision_lock(cloud_store, 0.05):
                    pytest.fail("the lock should not have been granted")
        finally:
            lock.release()
        assert "decisions" in str(excinfo.value)

    def test_kernel_writers_keep_their_unbounded_wait(self):
        """The bounded acquisition is the sync layer's own policy; the kernel
        path must not have been narrowed with it."""
        import inspect

        from nauro.store import store_lock

        source = inspect.getsource(store_lock.store_write_lock)
        assert "timeout" not in source

    def test_session_start_hook_skips_when_a_decision_writer_holds_the_lock(
        self, cloud_store, caplog
    ):
        manifest = _ok(200, {"files": [], "next_cursor": None})
        lock = self._hold_decisions(cloud_store)
        try:
            with (
                patch("nauro.sync.remote.httpx.Client.get", return_value=manifest),
                caplog.at_level(logging.INFO, logger="nauro.sync"),
            ):
                pulled = pull_before_session(CLOUD_PID, cloud_store)
        finally:
            lock.release()

        assert pulled == 0
        assert any("skipped" in record.getMessage() for record in caplog.records)

    def test_cli_sync_fails_loud_when_a_decision_writer_holds_the_lock(
        self, cloud_store, tmp_path, monkeypatch
    ):
        manifest = _ok(200, {"files": [], "next_cursor": None})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "nauro.sync.pull.decision_lock",
            lambda store_path, _timeout: decision_lock(store_path, 0.05),
        )
        lock = self._hold_decisions(cloud_store)
        try:
            with patch("nauro.sync.remote.httpx.Client.get", return_value=manifest):
                result = runner.invoke(app, ["sync"])
        finally:
            lock.release()

        assert result.exit_code == 1, result.output
        assert "decisions lock" in result.output
