"""Serialization guarantees for config.json read-modify-write.

config.json holds the auth token and the telemetry identity. Concurrent
token refresh racing a manual auth/logout, or the single-threaded
logout -> telemetry-rotation path, could previously lose a write. These
tests pin the contract of ``config_transaction``: it holds an exclusive
file lock, reloads fresh inside the lock, and persists only on clean exit.
All assertions are deterministic — no sleep-racing.
"""

from __future__ import annotations

import pytest
from filelock import FileLock, Timeout

from nauro.store.config import (
    _config_file,
    config_transaction,
    load_config,
    save_config,
)


def _lock_path():
    return _config_file().with_suffix(".lock")


def test_lock_is_held_during_body_and_released_after(tmp_path):
    """The config lock is present and held inside the body, released on exit."""
    seen_locked = False
    with config_transaction():
        # A fresh FileLock instance cannot acquire while the body holds the lock.
        contender = FileLock(str(_lock_path()))
        try:
            contender.acquire(timeout=0.2)
        except Timeout:
            seen_locked = True
        else:
            contender.release()
    assert seen_locked, "lock was not held during the transaction body"

    # After exit, a fresh instance acquires immediately.
    after = FileLock(str(_lock_path()))
    after.acquire(timeout=0.2)
    assert after.is_locked
    after.release()


def test_sequential_transactions_each_reload_fresh(tmp_path):
    """Two transactions mutating different keys both survive — no stale overwrite."""
    with config_transaction() as data:
        data["auth"] = {"access_token": "tok"}

    with config_transaction() as data:
        section = data.get("telemetry") or {}
        section["enabled"] = True
        data["telemetry"] = section

    final = load_config()
    assert final["auth"] == {"access_token": "tok"}
    assert final["telemetry"]["enabled"] is True


def test_logout_sequence_preserves_rotation_and_removes_auth(tmp_path):
    """Regression pin for the logout clobber.

    logout runs two sequential standalone transactions: rotate
    ``telemetry.anonymous_id`` first, then remove ``auth``. Because each reloads
    fresh inside the lock, the auth-removal transaction does not re-persist a
    pre-rotation snapshot — the rotated id survives and auth is gone.
    """
    save_config(
        {
            "auth": {"access_token": "tok"},
            "telemetry": {"anonymous_id": "old-id", "enabled": True},
        }
    )

    # Transaction 1: rotate the anonymous_id.
    with config_transaction() as data:
        section = data.get("telemetry") or {}
        section["anonymous_id"] = "new-id"
        data["telemetry"] = section

    # Transaction 2: remove auth (reloads fresh, so it sees the rotated id).
    with config_transaction() as data:
        data.pop("auth", None)

    final = load_config()
    assert "auth" not in final, "auth survived the removal transaction"
    assert final["telemetry"]["anonymous_id"] == "new-id", "rotated id was clobbered"
    assert final["telemetry"]["enabled"] is True


def test_second_acquirer_blocks_until_release(tmp_path):
    """A separate lock instance times out while a transaction holds the lock."""
    with config_transaction():
        contender = FileLock(str(_lock_path()))
        with pytest.raises(Timeout):
            contender.acquire(timeout=0.2)

    # Once the transaction has exited, the same contender acquires.
    contender = FileLock(str(_lock_path()))
    contender.acquire(timeout=0.2)
    assert contender.is_locked
    contender.release()


def test_transaction_preserves_owner_only_permissions(tmp_path):
    """A transaction write keeps config.json at 0o600."""
    with config_transaction() as data:
        data["auth"] = {"access_token": "tok"}

    config_path = _config_file()
    assert oct(config_path.stat().st_mode & 0o777) == "0o600"


def test_failed_body_leaves_disk_unchanged(tmp_path):
    """A body that raises skips the save — the file is byte-identical."""
    save_config({"auth": {"access_token": "tok"}})
    before = _config_file().read_bytes()

    with pytest.raises(RuntimeError):
        with config_transaction() as data:
            data["auth"] = {"access_token": "mutated"}
            raise RuntimeError("boom")

    assert _config_file().read_bytes() == before
