from datetime import datetime, timezone
from hashlib import sha256

import pytest

from nauro.store import recovery_actions as records

PROJECT = "01K00000000000000000000001"
ACTOR = "01K00000000000000000000002"
SAGA = "01K00000000000000000000003"
BINDING = "a" * 64
CREATED = "2026-09-16T10:00:00.000000Z"
DEADLINE = "2026-09-17T10:00:00.000000Z"


def action(disposition="resume", **changes):
    payload = records.RecoveryPayload.model_validate(
        {
            "schema": "nauro.judgment_recovery.v2",
            "saga_id": SAGA,
            "disposition": disposition,
            "binding": {
                "original_scope": {
                    "project_id": PROJECT,
                    "user_id": ACTOR,
                    "operation_kind": "judgment_commit",
                    "operation_id": "original",
                },
                "original_payload_digest": "b" * 64,
                "expected_state": "recovery_required",
                "expected_fence": 2,
                "expected_lease_owner": None,
                "expected_lease_expires_at": None,
                "created_at": CREATED,
                "admission_deadline": DEADLINE,
            },
        }
    ).canonical()
    return records.RecoveryAction.model_validate(
        {
            "version": 1,
            "connection_binding": BINDING,
            "project_id": PROJECT,
            "actor_id": ACTOR,
            "action_id": "recovery:test",
            "action_payload": payload,
            "payload_digest": sha256(payload.encode()).hexdigest(),
            "execution_deadline": DEADLINE,
            **changes,
        }
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    return records.RecoveryActionStore(BINDING, PROJECT, ACTOR)


def test_restart_discovers_exact_record_without_remembered_id(store):
    saved = action()
    store.save(saved)
    restarted = records.RecoveryActionStore(BINDING, PROJECT, ACTOR)
    assert restarted.list() == (saved,)
    assert restarted.read(saved.action_id).action_payload == saved.action_payload
    assert store._path(saved.action_id).stat().st_mode & 0o777 == 0o600


def test_existing_identity_cannot_be_replaced(store):
    store.save(action())
    with pytest.raises(FileExistsError):
        store.save(action("abandon"))
    assert store.read(action().action_id) == action()


@pytest.mark.parametrize(
    "change",
    [
        {"payload_digest": "c" * 64},
        {"action_payload": "{}"},
        {"project_id": ACTOR},
        {"action_id": ""},
        {"execution_deadline": "tomorrow"},
    ],
)
def test_invalid_record_refuses(change):
    with pytest.raises(ValueError):
        action(**change)


@pytest.mark.parametrize("change", [{"actor_id": PROJECT}, {"connection_binding": "c" * 64}])
def test_other_scope_refuses_save(store, change):
    with pytest.raises(ValueError):
        store.save(action(**change))
    assert store.list() == ()


@pytest.mark.parametrize(
    "damage", ["truncated", "duplicate", "symlink", "permissions", "directory"]
)
def test_corrupt_evidence_refuses_discovery(store, tmp_path, damage):
    saved = action()
    store.save(saved)
    path = store._path(saved.action_id)
    if damage == "truncated":
        path.write_bytes(b'{"version":')
    elif damage == "duplicate":
        path.write_bytes(path.read_bytes().replace(b'{"version":1', b'{"version":1,"version":1'))
    elif damage == "permissions":
        path.chmod(0o644)
    else:
        path.unlink()
        if damage == "symlink":
            other = tmp_path / "other"
            other.write_text(saved.model_dump_json())
            path.symlink_to(other)
        else:
            path.mkdir()
    with pytest.raises((ValueError, OSError)):
        store.list()


def test_record_reestablishes_barrier_after_final_sync_failure(store, monkeypatch):
    saved = action()
    original = records._directory_sync
    with monkeypatch.context() as fault:

        def sync(path):
            if store._path(saved.action_id).exists():
                raise OSError("interrupted barrier")
            original(path)

        fault.setattr(records, "_directory_sync", sync)
        with pytest.raises(OSError):
            store.save(saved)
    assert store.read(saved.action_id) == saved


@pytest.mark.parametrize("when", ["2026-09-16T09:59:59.000000Z", DEADLINE])
def test_action_horizon_cannot_be_extended(when):
    with pytest.raises(ValueError):
        action().require_window(records.timestamp(when))


def test_abandon_remains_possible_after_original_execution_deadline():
    past = "2026-09-16T09:00:00.000000Z"
    now = datetime(2026, 9, 16, 11, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        action(execution_deadline=past).require_window(now)
    action("abandon", execution_deadline=past).require_window(now)
