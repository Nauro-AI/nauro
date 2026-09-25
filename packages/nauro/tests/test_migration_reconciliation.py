from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from nauro.store.migration_admission import (
    MigrationAdmissionError,
    admission_path,
    inspect_migration,
    migration_home,
    migration_lock,
    require_migration_admission,
    retained_source,
)
from nauro.sync import migration_admission as admission
from nauro.sync import migration_installation as installation
from nauro.sync import migration_preservation as preservation
from nauro.sync.generation_acquisition import acquire_generation_projection
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.migration_admission import decide_migration_assessment, load_migration_plan
from nauro.sync.migration_reconciliation import reconcile_admitted_migration
from tests.test_guided_generation_upgrade import _move_target, _tree
from tests.test_migration_preservation import saved as _saved


@pytest.fixture
def saved(tmp_path, monkeypatch):
    yield from _saved.__wrapped__(tmp_path, monkeypatch)


def _fresh(session):
    return InitialAttachmentSession(
        session.binding, session.repo, session.connection, session.client
    )


def _stop(*args, **kwargs):
    raise OSError("interrupted")


def _admit(saved, monkeypatch, phase):
    record, plan, session, *_ = saved
    store = session.binding.store_path
    if phase == "blocked":
        return record
    install = installation.install_generation_root
    target, name, fault = {
        "replacing": (installation.os, "rename", _stop),
        "staging": (
            installation,
            "install_generation_root",
            lambda *a, **k: _stop(install(*a, **k)),
        ),
    }.get(phase, (installation, "_install", _stop))
    original = getattr(target, name)
    monkeypatch.setattr(target, name, fault)
    with pytest.raises(OSError):
        installation.continue_migration_installation(record, plan.assessment.projection, session)
    monkeypatch.setattr(target, name, original)
    current = inspect_migration(store)
    assert current.phase == ("replacing" if phase == "replacing" else "installing")
    return current


def _refused(saved, current, match):
    session = saved[2]
    before = _tree(session.binding.store_path.parent)
    with pytest.raises(MigrationAdmissionError, match=match):
        reconcile_admitted_migration(current, _fresh(session))
    assert _tree(session.binding.store_path.parent) == before
    assert inspect_migration(session.binding.store_path) == current


def test_target_moved_in_blocked_reconciles_to_reassessed(saved):
    record, plan, session, *_ = saved
    store = session.binding.store_path
    before = _tree(store.parent)
    identity = _move_target(saved)
    successor = reconcile_admitted_migration(record, session)
    assert (successor.phase, successor.source_id) == ("reassessed", None)
    assert successor.migration_id != record.migration_id
    assert successor.predecessor_digest == hashlib.sha256(record.canonical_bytes()).hexdigest()
    _, raw = load_migration_plan(store)
    assert json.loads(raw)["projection"]["generation_id"] == identity.generation_id
    assert admission.inspect_previous_migration(successor) == record
    assert _tree(store.parent) == before
    with pytest.raises(MigrationAdmissionError, match="incomplete"):
        require_migration_admission(store)


def test_target_moved_in_installing_absent_store_reconciles(saved, monkeypatch):
    session = saved[2]
    current = _admit(saved, monkeypatch, "installing")
    before = _tree(session.binding.store_path.parent)
    _move_target(saved)
    successor = reconcile_admitted_migration(current, _fresh(session))
    assert (successor.phase, successor.source_id) == ("reassessed", current.migration_id)
    assert retained_source(successor) == retained_source(current)
    assert _tree(session.binding.store_path.parent) == before


def test_target_moved_in_installing_with_staging_refuses_intact(saved, monkeypatch):
    current = _admit(saved, monkeypatch, "staging")
    assert any(saved[2].binding.store_path.iterdir())
    _move_target(saved)
    _refused(saved, current, "installation evidence for the earlier record is present")


def test_changed_source_in_blocked_reconciles_with_changed_paths(saved, monkeypatch):
    record, plan, session, *_ = saved
    store = session.binding.store_path
    copy = preservation._copy
    last = plan.entries[-1].source_path
    monkeypatch.setattr(
        preservation, "_copy", lambda s, r, e: _stop() if e.source_path == last else copy(s, r, e)
    )
    with pytest.raises(OSError):
        preservation.preserve_migration_source(record, session)
    monkeypatch.setattr(preservation, "_copy", copy)
    partial = _tree(plan.backup_root)
    (store / "state_current.md").write_text("Changed while blocked")
    successor = reconcile_admitted_migration(record, _fresh(session))
    _, raw = load_migration_plan(store)
    entries = {e["source_path"]: e["sha256"] for e in json.loads(raw)["entries"]}
    old = {e.source_path: e.sha256 for e in plan.entries}
    assert [path for path in old if entries[path] != old[path]] == ["state_current.md"]
    assert entries["state_current.md"] == hashlib.sha256(b"Changed while blocked").hexdigest()
    assert (successor.phase, successor.source_id) == ("reassessed", None)
    assert _tree(plan.backup_root) == partial


@pytest.mark.parametrize("phase", ["replacing", "installing"])
def test_changed_retained_source_refuses(saved, monkeypatch, phase):
    current = _admit(saved, monkeypatch, phase)
    source = saved[2].binding.store_path
    if phase == "installing":
        source = retained_source(current)
    (source / "state_current.md").write_text("Changed after admission")
    _move_target(saved)
    _refused(saved, current, "files changed after this upgrade was admitted")


def test_unchanged_target_and_source_refuses(saved):
    _refused(saved, saved[0], "Nothing changed")


@pytest.mark.parametrize("phase", ["blocked", "installing"])
def test_stale_old_record_continue_refuses_after_supersession(saved, monkeypatch, phase):
    session = saved[2]
    old = _admit(saved, monkeypatch, phase)
    _move_target(saved)
    successor = reconcile_admitted_migration(old, _fresh(session))
    before = _tree(session.binding.store_path.parent)
    monkeypatch.setattr(preservation, "_copy", _stop)
    monkeypatch.setattr(installation.os, "rename", _stop)
    fresh = _fresh(session)
    projection = acquire_generation_projection(
        fresh.binding, active_user_id=old.actor, session=fresh
    )
    with pytest.raises(MigrationAdmissionError, match="changed|Inspect the current"):
        installation.continue_migration_installation(old, projection, fresh)
    with pytest.raises(MigrationAdmissionError):
        decide_migration_assessment(old, preserve=True)
    assert _tree(session.binding.store_path.parent) == before
    assert inspect_migration(session.binding.store_path) == successor


def test_legacy_write_blocked_at_every_step(saved, monkeypatch):
    record, _, session, *_ = saved
    store = session.binding.store_path
    seen = []

    def guarded(name):
        original = getattr(admission, name)

        def hook(*args, **kwargs):
            with pytest.raises(MigrationAdmissionError, match="incomplete"):
                require_migration_admission(store)
            seen.append(name)
            result = original(*args, **kwargs)
            with pytest.raises(MigrationAdmissionError, match="incomplete"):
                require_migration_admission(store)
            return result

        monkeypatch.setattr(admission, name, hook)

    for name in ("_retain_previous", "atomic_write_bytes", "durable_replace"):
        guarded(name)
    _move_target(saved)
    successor = reconcile_admitted_migration(record, session)
    blocked = decide_migration_assessment(successor, preserve=True)
    assert (
        seen
        == ["_retain_previous", "durable_replace", "atomic_write_bytes"] + ["durable_replace"] * 2
    )
    assert blocked.phase == "blocked"
    with pytest.raises(MigrationAdmissionError, match="incomplete"):
        require_migration_admission(store)


def test_old_backup_proof_rejects_altered_backup(saved):
    record, plan, session, *_ = saved
    root = preservation.preserve_migration_source(record, session)
    (root / plan.entries[0].destination_path).write_bytes(b"altered")
    _move_target(saved)
    _refused(saved, record, "Preserved evidence differs")
    assert (root / plan.entries[0].destination_path).read_bytes() == b"altered"


def test_old_backup_retained_and_successor_copies_fresh_backup(saved):
    record, plan, session, *_ = saved
    root = preservation.preserve_migration_source(record, session)
    evidence = _tree(root)
    _move_target(saved)
    successor = decide_migration_assessment(
        reconcile_admitted_migration(record, _fresh(session)), preserve=True
    )
    _, raw = load_migration_plan(session.binding.store_path)
    fresh = _fresh(session)
    new_root = preservation.preserve_migration_source(successor, fresh)
    assert new_root != root
    assert (new_root / "plan.json").read_bytes() == raw
    for entry in plan.entries:
        assert (new_root / entry.destination_path).read_bytes() == (
            root / entry.destination_path
        ).read_bytes()
    assert _tree(root) == evidence


def test_predecessor_record_and_plan_retained_and_verified_on_load(saved):
    record, plan, session, *_ = saved
    store = session.binding.store_path
    _move_target(saved)
    successor = reconcile_admitted_migration(record, session)
    history = migration_home() / f"migration-history-{successor.predecessor_digest}.json"
    assert history.read_bytes() == record.canonical_bytes()
    assert admission._read_plan(record) == plan.manifest_json
    history.unlink()
    with pytest.raises(MigrationAdmissionError, match="predecessor evidence"):
        load_migration_plan(store)


def test_successor_source_id_forgery_refused(saved, monkeypatch):
    session = saved[2]
    current = _admit(saved, monkeypatch, "installing")
    _move_target(saved)
    successor = reconcile_admitted_migration(current, _fresh(session))
    for forged in (None, successor.migration_id):
        with pytest.raises(MigrationAdmissionError, match="same binding"):
            admission._require_replacement_binding(
                current, successor.model_copy(update={"source_id": forged})
            )
    path = admission_path(session.binding.store_path)
    path.write_bytes(successor.model_copy(update={"source_id": None}).canonical_bytes())
    with pytest.raises(MigrationAdmissionError, match="same binding"):
        load_migration_plan(session.binding.store_path)
    with pytest.raises(MigrationAdmissionError, match="same binding"):
        admission._require_replacement_binding(
            saved[0], successor.model_copy(update={"phase": "assessed"})
        )


def test_relocated_successor_consent_installs_without_rename(saved, monkeypatch):
    session = saved[2]
    store = session.binding.store_path
    current = _admit(saved, monkeypatch, "installing")
    relocated = _tree(retained_source(current))
    _move_target(saved)
    successor = reconcile_admitted_migration(current, _fresh(session))
    blocked = decide_migration_assessment(successor, preserve=True)
    monkeypatch.setattr(installation.os, "rename", _stop)
    fresh = _fresh(session)
    projection = acquire_generation_projection(
        fresh.binding, active_user_id=blocked.actor, session=fresh
    )
    completed = installation.continue_migration_installation(blocked, projection, fresh)
    assert (completed.phase, completed.migration_id) == ("completed", successor.migration_id)
    assert _tree(retained_source(completed)) == relocated
    assert (store / ".replica/authority.json").is_file()


def test_in_place_successor_decline_goes_declined(saved):
    record, _, session, *_ = saved
    store = session.binding.store_path
    before = _tree(store)
    _move_target(saved)
    declined = decide_migration_assessment(
        reconcile_admitted_migration(record, session), preserve=False
    )
    assert declined.phase == "declined"
    require_migration_admission(store)
    assert _tree(store) == before


def test_relocated_successor_decline_refuses(saved, monkeypatch):
    current = _admit(saved, monkeypatch, "installing")
    _move_target(saved)
    successor = reconcile_admitted_migration(current, _fresh(saved[2]))
    with pytest.raises(MigrationAdmissionError, match="cannot be deferred"):
        decide_migration_assessment(successor, preserve=False)
    assert inspect_migration(saved[2].binding.store_path) == successor


def test_busy_lock_refuses_reconciliation(saved):
    record, _, session, *_ = saved
    store = session.binding.store_path
    _move_target(saved)
    entered, release = Event(), Event()

    def hold():
        with migration_lock(store):
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        try:
            assert entered.wait(10)
            _refused(saved, record, "busy")
        finally:
            release.set()
        future.result(timeout=10)
