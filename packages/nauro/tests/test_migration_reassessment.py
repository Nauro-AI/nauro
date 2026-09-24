import hashlib

import pytest

from nauro.store.generation_migration_assessment import assess_legacy_migration
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import MigrationAdmissionError, inspect_migration
from nauro.sync import migration_admission as migration
from tests.test_migration_admission import saved as saved_fixture

saved = saved_fixture


def _next(plan):
    return prepare_legacy_migration_plan(assess_legacy_migration(plan.assessment.projection))


@pytest.mark.parametrize("declined", [False, True])
def test_replacement_preserves_predecessor_and_needs_fresh_disposition(saved, declined):
    binding, plan, old = saved
    if declined:
        old = migration.decide_migration_assessment(old, preserve=False)
    replacement = _next(plan)
    current = migration.save_migration_assessment(replacement, replace=old)
    assert current.phase == "assessed"
    assert current.migration_id == replacement.migration_id
    assert current.migration_id != old.migration_id
    assert current.predecessor_digest == hashlib.sha256(old.canonical_bytes()).hexdigest()
    assert migration.inspect_previous_migration(current) == old
    assert migration._read_plan(old) == plan.manifest_json
    assert migration.load_migration_plan(binding.store_path) == (current, replacement.manifest_json)
    assert migration.save_migration_assessment(replacement, replace=old) == current
    with pytest.raises(
        MigrationAdmissionError, match=("exact assessed" if declined else "disposition changed")
    ):
        migration.decide_migration_assessment(old, preserve=True)


def test_blocked_migration_cannot_be_replaced(saved):
    binding, plan, old = saved
    blocked = migration.decide_migration_assessment(old, preserve=True)
    replacement = _next(plan)
    for expected in (old, blocked):
        with pytest.raises(MigrationAdmissionError):
            migration.save_migration_assessment(replacement, replace=expected)
    assert inspect_migration(binding.store_path) == blocked
    assert (
        not migration._plan_path(blocked)
        .with_name(f"migration-plan-{replacement.migration_id}.json")
        .exists()
    )


def test_two_replacements_cannot_both_win(saved):
    binding, plan, old = saved
    first, second = _next(plan), _next(plan)
    winner = migration.save_migration_assessment(first, replace=old)
    with pytest.raises(MigrationAdmissionError, match="retained migration"):
        migration.save_migration_assessment(second, replace=old)
    assert inspect_migration(binding.store_path) == winner
    assert migration.inspect_previous_migration(winner) == old


def test_predecessor_barrier_failure_preserves_current_record(saved, monkeypatch):
    binding, plan, old = saved
    original = migration.sync_file

    def fail_history(paths, path):
        if path.name.startswith("migration-history-"):
            raise OSError("history barrier")
        return original(paths, path)

    monkeypatch.setattr(migration, "sync_file", fail_history)
    with pytest.raises(OSError, match="history barrier"):
        migration.save_migration_assessment(_next(plan), replace=old)
    assert inspect_migration(binding.store_path) == old
    assert migration._read_plan(old) == plan.manifest_json


def test_lost_replacement_response_recovers_same_identity(saved, monkeypatch):
    binding, plan, old = saved
    replacement = _next(plan)
    original = migration.durable_replace

    def lose_response(paths, path, raw):
        original(paths, path, raw)
        if path == migration.admission_path(binding.store_path):
            raise OSError("response lost")

    monkeypatch.setattr(migration, "durable_replace", lose_response)
    with pytest.raises(OSError, match="response lost"):
        migration.save_migration_assessment(replacement, replace=old)
    current = inspect_migration(binding.store_path)
    assert current.migration_id == replacement.migration_id
    assert migration.inspect_previous_migration(current) == old
    monkeypatch.setattr(migration, "durable_replace", original)
    assert migration.save_migration_assessment(replacement, replace=old) == current


def test_changed_binding_cannot_reuse_replacement_consent(saved):
    binding, plan, old = saved
    other = old.model_copy(update={"actor": "01K99999999999999999999999"})
    with pytest.raises(MigrationAdmissionError, match="same binding"):
        migration.save_migration_assessment(_next(plan), replace=other)
    assert inspect_migration(binding.store_path) == old


def test_missing_predecessor_refuses_disposition(saved):
    binding, plan, old = saved
    current = migration.save_migration_assessment(_next(plan), replace=old)
    migration._previous_path(current.predecessor_digest).unlink()
    with pytest.raises(MigrationAdmissionError, match="predecessor"):
        migration.decide_migration_assessment(current, preserve=True)
    assert inspect_migration(binding.store_path) == current
