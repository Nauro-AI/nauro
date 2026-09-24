from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
from threading import Event

import pytest
from filelock import FileLock, Timeout
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import recovery, registry
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import (
    MigrationAdmissionError,
    admission_path,
    inspect_migration,
    migration_lock_path,
    migration_write_guard,
)
from nauro.store.repo_config import save_repo_config
from nauro.sync import migration_admission as migration
from nauro.sync.push import PushReport
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_generation_migration_plan import _assessment

PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    def forbidden(*args, **kwargs):
        pytest.fail("External network is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    return repo


def _initialize():
    result = CliRunner().invoke(app, ["init", "Synthetic"])
    assert result.exit_code == 0, result.output
    pid, _ = registry.find_projects_by_name_v2("Synthetic")[0]
    return registry.get_store_path_v2(pid)


@pytest.mark.parametrize("route", ["attach", "reconnect", "demo", "add_repo", "purge", "link"])
def test_setup_holds_fence_through_binding_and_ancillary_work(
    isolated, tmp_path, monkeypatch, route
):
    module_name = {"demo": "init", "add_repo": "init", "purge": "adopt"}.get(route, route)
    module = import_module(f"nauro.cli.commands.{module_name}")
    stores = []
    if route in {"attach", "reconnect"}:
        store = registry.get_store_path_v2(PID)
        monkeypatch.setattr(module, "require_cloud_membership", lambda *_: "Synthetic")
        if route == "attach":
            registry.register_project_v2(
                "Synthetic", [], project_id=PID, mode="cloud", server_url="https://synthetic.test"
            )
            scaffold_project_store("Synthetic", store)
            arguments, seam = ["attach", PID], "warn_then_regen"
        else:
            save_repo_config(
                isolated,
                {
                    "id": PID,
                    "name": "Synthetic",
                    "mode": "cloud",
                    "server_url": "https://synthetic.test",
                },
            )

            def restore(_pid, destination, _reporter):
                scaffold_project_store("Synthetic", destination)
                return destination

            monkeypatch.setattr(module, "restore_cloud_store", restore)
            arguments, seam = ["reconnect"], "_finish_connection"
    else:
        store = _initialize()
        if route in {"demo", "add_repo"}:
            extra = tmp_path / "extra"
            extra.mkdir()
            monkeypatch.chdir(extra)
            arguments = (
                ["init", "Synthetic", "--demo"]
                if route == "demo"
                else [
                    "init",
                    "Synthetic",
                    "--add-repo",
                    str(extra),
                ]
            )
            seam = "_regenerate_after_init"
        elif route == "purge":
            monkeypatch.setattr(module, "setup_all_surfaces", lambda *a, **k: [])
            arguments, seam = ["adopt", "--remove", "--purge-store", "--yes"], "remove_project_v2"
        else:
            monkeypatch.setattr(module, "load_access_token", lambda: "synthetic")
            monkeypatch.setattr(module, "create_project", lambda *_: {"project_id": PID})
            monkeypatch.setattr(module, "push_changed_files", lambda *a, **k: PushReport())
            stores.append(store.with_name(PID))
            arguments, seam = ["link", "--cloud"], "push_changed_files"
    stores.append(store)
    original = getattr(module, seam)
    entered, release = Event(), Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, seam, delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(CliRunner().invoke, app, arguments, input="restore\n")
        try:
            assert entered.wait(10)
            for target in stores:
                with pytest.raises(Timeout), FileLock(migration_lock_path(target), timeout=0):
                    pytest.fail("Conversion entered an active setup operation")
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result.exit_code == 0, result.output
    for target in stores:
        with FileLock(migration_lock_path(target), timeout=0):
            pass


def test_reconnect_locate_holds_both_original_and_selected_paths(isolated, tmp_path, monkeypatch):
    module = import_module("nauro.cli.commands.reconnect")
    save_repo_config(isolated, {"id": PID, "name": "Synthetic", "mode": "local"})
    original_store = registry.get_store_path_v2(PID)
    selected = tmp_path / "external" / PID
    scaffold_project_store("Synthetic", selected)
    original = module._finish_connection

    def check_fences(*args):
        def competing():
            for store in (original_store, selected):
                with pytest.raises(Timeout), FileLock(migration_lock_path(store), timeout=0):
                    pytest.fail("Rebinding was not excluded")

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(competing).result(timeout=10)
        return original(*args)

    monkeypatch.setattr(module, "_finish_connection", check_fences)
    result = CliRunner().invoke(app, ["reconnect"], input=f"locate\n{selected}\n")
    assert result.exit_code == 0, result.output


def test_home_alias_reuses_control_files_and_keeps_blocked_record_visible(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("NAURO_HOME", str(home))
    binding, assessment = _assessment(tmp_path)
    plan = prepare_legacy_migration_plan(assessment)
    record = migration.save_migration_assessment(plan)
    before = admission_path(binding.store_path), migration_lock_path(binding.store_path)
    monkeypatch.setenv("NAURO_HOME", str(alias))
    assert before == (admission_path(binding.store_path), migration_lock_path(binding.store_path))
    assert migration.load_migration_plan(binding.store_path) == (record, plan.manifest_json)
    blocked = migration.decide_migration_assessment(record, preserve=True)
    assert inspect_migration(binding.store_path) == blocked
    with (
        pytest.raises(MigrationAdmissionError, match="conversion is incomplete"),
        migration_write_guard(binding.store_path),
    ):
        pytest.fail("Home alias bypassed admission")
    monkeypatch.setenv("NAURO_HOME", str(home))
    assert inspect_migration(binding.store_path) == blocked


def test_raw_restore_holds_fence_and_blocked_restore_preserves_evidence(
    isolated, tmp_path, monkeypatch
):
    binding, assessment = _assessment(tmp_path)
    record = migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
    binding.store_path.rename(binding.store_path.with_name("retained"))

    def empty_manifest(*args, **kwargs):
        def competing():
            with pytest.raises(Timeout):
                migration.decide_migration_assessment(record, preserve=True)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(competing).result(timeout=10)
        return []

    monkeypatch.setattr(recovery, "fetch_manifest", empty_manifest)
    with pytest.raises(recovery.EmptyCloudRecordError):
        recovery.restore_cloud_store(binding.project_id, binding.store_path)
    binding.store_path.with_name("retained").rename(binding.store_path)
    migration.decide_migration_assessment(record, preserve=True)
    binding.store_path.rename(binding.store_path.with_name("retained"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(MigrationAdmissionError, match="conversion is incomplete"):
        recovery.restore_cloud_store(binding.project_id, binding.store_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not binding.store_path.exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_default_home_alias_preserves_record_and_fence(tmp_path, monkeypatch, legacy):
    import hashlib

    home = tmp_path / "home"
    home.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("NAURO_HOME", str(home))
    binding, assessment = _assessment(home)
    plan = prepare_legacy_migration_plan(assessment)
    record = migration.save_migration_assessment(plan)
    physical_store = registry.get_store_path_v2(binding.project_id)
    monkeypatch.setenv("NAURO_HOME", str(alias))
    aliased_store = registry.get_store_path_v2(binding.project_id)
    assert aliased_store != physical_store
    path = admission_path(physical_store)
    if legacy:
        path.unlink()
        record = record.model_copy(update={"store": str(aliased_store)})
        key = hashlib.sha256(str(aliased_store.absolute()).encode()).hexdigest()
        path = home / f"migration-{key}.json"
        path.write_bytes(record.canonical_bytes())
    assert inspect_migration(aliased_store) == record
    assert inspect_migration(physical_store) == record
    assert admission_path(physical_store) == path
    assert path.read_bytes() == record.canonical_bytes()
    assert migration.save_migration_assessment(plan) == record
    assert path.read_bytes() == record.canonical_bytes()
    assert migration_lock_path(aliased_store) == migration_lock_path(physical_store)
    with migration_write_guard(physical_store), ThreadPoolExecutor(max_workers=1) as pool:

        def contender():
            with (
                pytest.raises(MigrationAdmissionError, match="busy"),
                migration_write_guard(aliased_store, timeout=0),
            ):
                pytest.fail("Alias entered the held fence")

        pool.submit(contender).result(timeout=10)
    blocked = migration.decide_migration_assessment(record, preserve=True)
    physical_store.rename(physical_store.with_name("retained"))
    assert inspect_migration(physical_store) == inspect_migration(aliased_store) == blocked
    assert path.read_bytes() == blocked.canonical_bytes()
    with (
        pytest.raises(MigrationAdmissionError, match="conversion is incomplete"),
        migration_write_guard(aliased_store),
    ):
        pytest.fail("Missing source reopened alias admission")
    if legacy:
        original_iterdir = Path.iterdir

        def refuse_enumeration(self):
            if self == home:
                raise PermissionError("Synthetic enumeration failure")
            return original_iterdir(self)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "iterdir", refuse_enumeration)
            with pytest.raises(MigrationAdmissionError, match="evidence is unavailable"):
                inspect_migration(physical_store)
        canonical_record = blocked.model_copy(update={"store": str(physical_store)})
        key = hashlib.sha256(str(physical_store).encode()).hexdigest()
        (home / f"migration-{key}.json").write_bytes(canonical_record.canonical_bytes())
        with pytest.raises(MigrationAdmissionError, match="evidence is unavailable"):
            inspect_migration(physical_store)


def test_unknown_or_excess_admission_evidence_refuses(tmp_path, monkeypatch):
    from nauro.store import migration_admission as controls

    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    path = tmp_path / ("migration-" + "a" * 64 + ".json")
    path.write_bytes(b"{")
    with pytest.raises(MigrationAdmissionError, match="evidence is unavailable"):
        inspect_migration(tmp_path / "projects" / PID)
    monkeypatch.setattr(controls, "MAX_ADMISSION_RECORDS", 0)
    with pytest.raises(MigrationAdmissionError, match="evidence is unavailable") as raised:
        inspect_migration(tmp_path / "projects" / PID)
    assert str(raised.value.__cause__) == "Migration admission inspection limit exceeded"
