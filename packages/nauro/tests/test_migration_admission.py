from __future__ import annotations

import json

import pytest
import typer

from nauro.cli.generation_writes import require_legacy_write
from nauro.mcp import tools
from nauro.store import migration_admission as controls
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import (
    MigrationAdmissionError,
    admission_path,
    inspect_migration,
    require_migration_admission,
)
from nauro.store.read_authority import observe_generation_marker, require_legacy_context
from nauro.sync import migration_admission as migration
from tests.test_generation_migration_plan import _assessment


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    binding, assessment = _assessment(tmp_path)
    plan = prepare_legacy_migration_plan(assessment)
    record = migration.save_migration_assessment(plan)
    return binding, plan, record


def test_saved_assessment_reopens_exact_plan_without_execution(saved):
    binding, plan, record = saved
    before = {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    }
    assert migration.load_migration_plan(binding.store_path) == (record, plan.manifest_json)
    assert migration.save_migration_assessment(plan) == record
    require_migration_admission(binding.store_path)
    assert not plan.backup_root.exists()
    assert before == {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("preserve", [True, False])
def test_explicit_disposition_replays_without_copying(saved, preserve):
    binding, plan, record = saved
    decided = migration.decide_migration_assessment(record, preserve=preserve)
    assert decided.phase == ("blocked" if preserve else "declined")
    assert migration.decide_migration_assessment(record, preserve=preserve) == decided
    assert migration.load_migration_plan(binding.store_path) == (decided, plan.manifest_json)
    assert not plan.backup_root.exists()
    if not preserve:
        require_migration_admission(binding.store_path)
    with pytest.raises(MigrationAdmissionError, match="disposition changed"):
        migration.decide_migration_assessment(record, preserve=not preserve)


@pytest.mark.parametrize("consumer", ["write", "read", "guidance", "tool_read", "tool_write"])
@pytest.mark.parametrize("missing", [False, True])
def test_blocked_record_refuses_ordinary_consumers(saved, consumer, missing):
    binding, plan, record = saved
    migration.decide_migration_assessment(record, preserve=True)
    if missing:
        binding.store_path.rename(binding.store_path.with_name("retained"))
    if consumer == "write":
        with pytest.raises(typer.Exit) as raised:
            require_legacy_write(binding.store_path, "note")
        assert raised.value.exit_code == 1
    elif consumer == "read":
        with pytest.raises(MigrationAdmissionError, match="conversion is incomplete"):
            observe_generation_marker(binding)
    elif consumer == "guidance":
        with pytest.raises(MigrationAdmissionError, match="conversion is incomplete"):
            require_legacy_context(binding.store_path)
    else:
        result = (
            tools.tool_get_decision(binding.store_path, number=1)
            if consumer == "tool_read"
            else tools.tool_propose_decision(binding.store_path, title="No", rationale="No")
        )
        assert result["status"] == "error"
        assert result["guidance"] == "Project conversion is incomplete; reopen connection setup."
    assert not plan.backup_root.exists()


@pytest.mark.parametrize("damage", ["json", "phase", "store", "project", "unknown", "symlink"])
def test_corrupt_admission_never_means_absent(saved, damage, tmp_path):
    binding, _, record = saved
    path = admission_path(binding.store_path)
    if damage == "json":
        path.write_bytes(b"{")
    elif damage == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    else:
        data = json.loads(path.read_bytes())
        data[
            {"phase": "phase", "store": "store", "project": "project_id", "unknown": "extra"}[
                damage
            ]
        ] = "01K99999999999999999999999" if damage == "project" else "bad"
        path.write_text(json.dumps(data))
    with pytest.raises(MigrationAdmissionError):
        require_migration_admission(binding.store_path)


def test_changed_plan_cannot_be_accepted(saved):
    binding, _, record = saved
    path = migration._plan_path(record)
    path.write_bytes(b"{}")
    with pytest.raises(MigrationAdmissionError, match="plan differs"):
        migration.decide_migration_assessment(record, preserve=True)
    assert inspect_migration(binding.store_path) == record


def test_lost_disposition_response_remains_blocked(saved, monkeypatch):
    binding, _, record = saved
    original = migration.durable_replace

    def interrupted(*args):
        original(*args)
        raise OSError("response lost")

    monkeypatch.setattr(migration, "durable_replace", interrupted)
    with pytest.raises(OSError, match="response lost"):
        migration.decide_migration_assessment(record, preserve=True)
    assert inspect_migration(binding.store_path).phase == "blocked"
    with pytest.raises(MigrationAdmissionError):
        require_migration_admission(binding.store_path)
    monkeypatch.setattr(migration, "durable_replace", original)
    assert migration.decide_migration_assessment(record, preserve=True).phase == "blocked"


def test_failed_plan_barrier_does_not_publish_disposition(saved, monkeypatch):
    binding, _, record = saved

    def interrupted(*args):
        raise OSError("fsync failed")

    monkeypatch.setattr(migration, "sync_file", interrupted)
    with pytest.raises(OSError, match="fsync failed"):
        migration.decide_migration_assessment(record, preserve=True)
    assert inspect_migration(binding.store_path) == record


@pytest.mark.parametrize("legacy", [False, True])
def test_nonempty_lock_refuses_without_truncation(saved, legacy):
    binding, _, record = saved
    path = (
        admission_path(binding.store_path).with_suffix(".lock")
        if legacy
        else controls.migration_lock_path(binding.store_path)
    )
    path.write_bytes(b"retain")
    with pytest.raises(MigrationAdmissionError, match="lock contains"):
        migration.decide_migration_assessment(record, preserve=True)
    assert path.read_bytes() == b"retain"


@pytest.mark.parametrize(
    "command",
    [
        ["get-decision", "1"],
        ["sync"],
        ["note", "Do not write"],
        ["repair"],
        ["update-state", "Do not write"],
    ],
)
def test_normal_cli_refuses_pending_conversion(saved, tmp_path, monkeypatch, command):
    from typer.testing import CliRunner

    from nauro.cli.main import app
    from nauro.store.registry import bind_project_store_v2
    from nauro.store.repo_config import save_repo_config

    binding, _, record = saved
    repo = tmp_path / "repo"
    repo.mkdir()
    bind_project_store_v2(
        project_id=binding.project_id,
        name="Migration",
        mode="cloud",
        repo_path=repo,
        store_path=binding.store_path,
        server_url=binding.server_url,
    )
    save_repo_config(
        repo,
        {
            "id": binding.project_id,
            "name": "Migration",
            "mode": "cloud",
            "server_url": binding.server_url,
        },
    )
    monkeypatch.chdir(repo)
    migration.decide_migration_assessment(record, preserve=True)
    before = {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    }
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1, result.output
    assert "conversion is incomplete" in result.output, result.output
    assert before == {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    }


def test_oversized_saved_plan_refuses_before_read(saved):
    binding, _, record = saved
    with migration._plan_path(record).open("wb") as handle:
        handle.truncate(migration.MAX_PLAN_BYTES + 1)
    with pytest.raises(MigrationAdmissionError, match="plan is unsafe"):
        migration.load_migration_plan(binding.store_path)


@pytest.mark.parametrize("operation", ["save", "decide", "writer"])
@pytest.mark.parametrize("replace", [False, True])
def test_late_lock_evidence_is_preserved_before_record_changes(
    saved, monkeypatch, operation, replace
):
    binding, plan, record = saved
    original = controls.migration_lock_path
    calls = 0
    path = original(binding.store_path)
    before = admission_path(binding.store_path).read_bytes()

    def restore(store):
        nonlocal calls
        result = original(store)
        calls += 1
        if calls == 1:
            if replace:
                path.unlink()
            path.write_bytes(b"restored evidence")
        return result

    monkeypatch.setattr(controls, "migration_lock_path", restore)
    with pytest.raises(MigrationAdmissionError, match="unsafe evidence"):
        if operation == "save":
            migration.save_migration_assessment(plan)
        elif operation == "decide":
            migration.decide_migration_assessment(record, preserve=True)
        else:
            with controls.migration_write_guard(binding.store_path):
                pytest.fail("Late evidence admitted a writer")
    assert path.read_bytes() == b"restored evidence"
    assert admission_path(binding.store_path).read_bytes() == before
    assert migration._read_plan(record) == plan.manifest_json


def test_configured_home_alias_preserves_admission(saved, tmp_path, monkeypatch):
    binding, plan, record = saved
    home = migration.migration_home()
    alias = tmp_path / "linked-home"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("NAURO_HOME", str(alias))
    require_migration_admission(binding.store_path)
    assert migration.load_migration_plan(binding.store_path) == (record, plan.manifest_json)
    assert migration.save_migration_assessment(plan) == record
    blocked = migration.decide_migration_assessment(record, preserve=True)
    with pytest.raises(MigrationAdmissionError, match="conversion is incomplete"):
        require_migration_admission(binding.store_path)
    assert inspect_migration(binding.store_path) == blocked


def test_configured_home_alias_without_record_allows_legacy_reads(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    alias = tmp_path / "linked-home"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("NAURO_HOME", str(alias))
    binding, _ = _assessment(tmp_path)
    assert inspect_migration(binding.store_path) is None
    require_legacy_context(binding.store_path)
