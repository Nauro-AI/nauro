from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from nauro.store.migration_admission import (
    MigrationAdmissionError,
    admission_path,
    inspect_migration,
    migration_lock_path,
    migration_write_guard,
)
from nauro.sync import migration_admission as migration
from tests.test_migration_admission import saved as saved_fixture

saved = saved_fixture


def test_case_alias_uses_same_lock_and_block_during_vacancy(saved):
    binding, _, record = saved
    store = binding.store_path
    alternate = store.parent.with_name(store.parent.name.upper()) / store.name
    same_parent = alternate.parent.exists() and alternate.parent.samefile(store.parent)
    if not same_parent:
        alternate.parent.mkdir()
    assert migration_lock_path(store) == migration_lock_path(alternate)
    with migration_write_guard(store), ThreadPoolExecutor(max_workers=1) as pool:

        def contender():
            with (
                pytest.raises(MigrationAdmissionError, match="busy"),
                migration_write_guard(alternate, timeout=0),
            ):
                pytest.fail("Case spelling split the project lock")

        pool.submit(contender).result(timeout=5)
    blocked = migration.decide_migration_assessment(record, preserve=True)
    for vacant in (False, True):
        if vacant:
            store.rename(store.with_name("retained-source"))
        if same_parent:
            assert inspect_migration(alternate) == blocked
        else:
            with pytest.raises(MigrationAdmissionError):
                inspect_migration(alternate)
        with (
            pytest.raises(MigrationAdmissionError),
            migration_write_guard(alternate, timeout=0),
        ):
            pytest.fail("Case spelling bypassed blocked admission")


@pytest.mark.parametrize("change", ["remove", "retarget"])
def test_uncertain_old_alias_never_hides_blocked_evidence(saved, tmp_path, change):
    binding, _, record = saved
    store = binding.store_path
    alias = tmp_path / "old-alias"
    alias.symlink_to(store.parent, target_is_directory=True)
    original = admission_path(store)
    old = record.model_copy(update={"store": str(alias / store.name), "phase": "blocked"})
    original.unlink()
    digest = hashlib.sha256(old.store.encode()).hexdigest()
    retained = migration.migration_home() / f"migration-{digest}.json"
    retained.write_bytes(old.canonical_bytes())
    assert inspect_migration(store) == old
    alias.unlink()
    if change == "retarget":
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / store.name).mkdir(parents=True)
        alias.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(MigrationAdmissionError):
        inspect_migration(store)
    with pytest.raises(MigrationAdmissionError), migration_write_guard(store):
        pytest.fail("Uncertain old binding bypassed admission")
    assert retained.read_bytes() == old.canonical_bytes()
    alias.unlink(missing_ok=True)
    alias.symlink_to(store.parent, target_is_directory=True)
    assert inspect_migration(store) == old
