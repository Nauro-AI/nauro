from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from typer.testing import CliRunner

from nauro.cli.commands import projects
from nauro.cli.main import app
from nauro.store.generation_migration_assessment import assess_legacy_migration
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import MigrationAdmissionError, inspect_migration
from nauro.store.registry import register_project_v2
from nauro.store.replica_control import ReplicaControlBusyError
from nauro.sync import migration_admission as migration
from tests.test_generation_migration_plan import _assessment


def reassess_after_writer(record, projection):
    with pytest.raises(MigrationAdmissionError, match="fresh assessment"):
        migration.decide_migration_assessment(record, preserve=True)
    assert inspect_migration(projection.target.binding.store_path) == record
    plan = prepare_legacy_migration_plan(assess_legacy_migration(projection))
    fresh = migration.save_migration_assessment(plan, replace=record)
    assert fresh.phase == "assessed"
    return migration.decide_migration_assessment(fresh, preserve=True)


@pytest.mark.parametrize(
    "change", ["same_size", "add", "delete", "directory", "pending", "link", "missing", "control"]
)
def test_changed_source_cannot_be_blocked_on_old_disposition(tmp_path, monkeypatch, change):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    plan = prepare_legacy_migration_plan(assessment)
    record = migration.save_migration_assessment(plan)
    store = binding.store_path
    state = store / "state_current.md"
    if change == "same_size":
        state.write_bytes(b"x" * state.stat().st_size)
    elif change == "add":
        (store / "unpublished.md").write_text("Preserve this")
    elif change == "delete":
        state.unlink()
    elif change == "directory":
        (store / "empty-evidence").mkdir()
    elif change == "pending":
        (store / ".pull-spool-interrupted").mkdir()
    elif change == "link":
        state.unlink()
        state.symlink_to(store / "project.md")
    elif change == "control":
        (store / ".replica-control.lock").write_text("Unknown evidence")
    else:
        store.rename(store.with_name("retained"))
    with pytest.raises(MigrationAdmissionError, match="fresh assessment"):
        migration.decide_migration_assessment(record, preserve=True)
    assert inspect_migration(store) == record
    assert migration.load_migration_plan(store) == (record, plan.manifest_json)
    assert not plan.backup_root.exists()
    assert migration.decide_migration_assessment(record, preserve=False).phase == "declined"


def test_exact_blocked_replay_is_inspection_not_source_reexecution(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    record = migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
    blocked = migration.decide_migration_assessment(record, preserve=True)
    binding.store_path.rename(binding.store_path.with_name("retained"))

    def forbidden(*args):
        pytest.fail("Replay re-executed source validation")

    monkeypatch.setattr(migration, "verify_migration_source", forbidden)
    assert migration.decide_migration_assessment(record, preserve=True) == blocked
    assert not binding.store_path.exists()


def test_registry_removal_holds_fence_and_blocked_record_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    record = migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
    register_project_v2(
        "Synthetic", [], project_id=binding.project_id, mode="cloud", server_url=binding.server_url
    )
    monkeypatch.setattr(projects, "registered_store_path_hint_v2", lambda *a: binding.store_path)
    entered, release = Event(), Event()
    original = projects.remove_project_v2

    def delayed(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(projects, "remove_project_v2", delayed)
    arguments = ["projects", "rm", binding.project_id, "--yes"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(CliRunner().invoke, app, arguments)
        try:
            assert entered.wait(10)
            with pytest.raises(ReplicaControlBusyError, match="busy"):
                migration.decide_migration_assessment(record, preserve=True)
        finally:
            release.set()
        assert future.result(timeout=10).exit_code == 0
    register_project_v2(
        "Synthetic", [], project_id=binding.project_id, mode="cloud", server_url=binding.server_url
    )
    migration.decide_migration_assessment(record, preserve=True)
    before = projects.load_registry_v2()
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 1
    assert "conversion is incomplete" in result.output
    assert projects.load_registry_v2() == before


def test_control_evidence_arriving_during_inventory_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    record = migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
    original = migration._inventory
    control = binding.store_path / ".replica-control.lock"

    def arriving(store):
        result = original(store)
        control.write_bytes(b"arriving evidence")
        return result

    monkeypatch.setattr(migration, "_inventory", arriving)
    with pytest.raises(MigrationAdmissionError, match="fresh assessment"):
        migration.decide_migration_assessment(record, preserve=True)
    assert control.read_bytes() == b"arriving evidence"
    assert inspect_migration(binding.store_path) == record
