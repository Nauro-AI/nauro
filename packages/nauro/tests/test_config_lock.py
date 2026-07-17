"""Serialization guarantees for config.json read-modify-write.

These tests pin the contract of ``config_transaction``: it holds an exclusive
file lock, reloads fresh inside the lock, preserves unrelated and legacy data,
and persists only on clean exit. All assertions are deterministic.
"""

from __future__ import annotations

import json

import pytest
from filelock import FileLock, Timeout

from nauro.store.config import (
    _config_file,
    config_transaction,
    load_config,
    save_config,
    set_config,
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
        data["search.embeddings"] = True

    final = load_config()
    assert final["auth"] == {"access_token": "tok"}
    assert final["search.embeddings"] is True


def test_auth_removal_preserves_legacy_telemetry_section(tmp_path):
    legacy_telemetry = {
        "anonymous_id": "legacy-id",
        "enabled": True,
        "unknown": {"preserve": [1, "two", None]},
    }
    save_config(
        {
            "auth": {"access_token": "tok"},
            "telemetry": legacy_telemetry,
        }
    )

    with config_transaction() as data:
        data.pop("auth", None)

    final = load_config()
    assert "auth" not in final
    assert final["telemetry"] == legacy_telemetry


def test_ordinary_config_write_preserves_serialized_legacy_telemetry_data(tmp_path):
    legacy_telemetry = {
        "anonymous_id": "legacy-id",
        "enabled": True,
        "unknown": {"preserve": [1, "two", None]},
    }
    before = json.dumps(
        legacy_telemetry,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    save_config({"telemetry": legacy_telemetry})

    set_config("search.embeddings", "true")

    after = json.dumps(
        load_config()["telemetry"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert after == before


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
