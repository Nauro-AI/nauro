from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from filelock import FileLock

from nauro.mcp import tools
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import inspect_migration, migration_lock, migration_lock_path
from nauro.store.replica_control import ReplicaControlBusyError
from nauro.sync import migration_admission as migration
from tests.test_generation_migration_plan import _assessment
from tests.test_migration_source_check import reassess_after_writer


@pytest.mark.parametrize("pause_at", ["operation", "post_commit", "journal"])
def test_conversion_refuses_until_whole_writer_finishes(tmp_path, monkeypatch, pause_at):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    record = migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
    entered, release = Event(), Event()
    monkeypatch.setattr(tools, "_try_push", lambda *_: None)
    name = {
        "operation": "_update_state_op",
        "post_commit": "run_post_commit",
        "journal": "_emit_write_event",
    }[pause_at]
    original = getattr(tools, name)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(tools, name, paused)
    before = (binding.store_path / "state_current.md").read_bytes()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.tool_update_state, binding.store_path, delta="Delayed writer")
        try:
            assert entered.wait(10)
            with pytest.raises(ReplicaControlBusyError):
                migration.decide_migration_assessment(record, preserve=True)
            assert inspect_migration(binding.store_path) == record
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "ok"
    assert (binding.store_path / "state_current.md").read_bytes() != before
    blocked = reassess_after_writer(record, assessment.projection)
    assert blocked.phase == "blocked"
    after = {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    }
    assert tools.tool_update_state(binding.store_path, delta="Too late")["status"] == "error"
    assert after == {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    }


def test_writer_checks_admission_after_waiting_for_conversion(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    record = migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
    entered = Event()
    original = FileLock.acquire

    def observed_acquire(self, *args, **kwargs):
        if self.lock_file == str(migration_lock_path(binding.store_path)):
            entered.set()
        return original(self, *args, **kwargs)

    with ThreadPoolExecutor(max_workers=1) as pool:
        lock = migration_lock(binding.store_path)
        lock.__enter__()
        try:
            monkeypatch.setattr(FileLock, "acquire", observed_acquire)
            future = pool.submit(tools.tool_update_state, binding.store_path, delta="Too late")
            assert entered.wait(10)
            assert migration.decide_migration_assessment(record, preserve=True).phase == "blocked"
        finally:
            lock.__exit__(None, None, None)
        result = future.result(timeout=10)
    assert result["status"] == "error"
    assert result["guidance"] == "Project conversion is incomplete; reopen connection setup."


def test_writer_before_assessment_also_holds_conversion_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    entered, release = Event(), Event()
    original = tools._update_state_op
    monkeypatch.setattr(tools, "_try_push", lambda *_: None)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(tools, "_update_state_op", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.tool_update_state, binding.store_path, delta="Earlier writer")
        try:
            assert entered.wait(10)
            with pytest.raises(ReplicaControlBusyError):
                migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
            assert inspect_migration(binding.store_path) is None
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "ok"
