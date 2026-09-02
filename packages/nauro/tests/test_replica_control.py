from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Literal

import pytest
from filelock import FileLock, Timeout

from nauro.store.generation_authority import (
    GenerationProjectAuthority,
    select_project_authority,
)
from nauro.store.replica_control import (
    ReplicaControlBusyError,
    ReplicaControlLayout,
    ReplicaControlReadError,
    ReplicaControlSnapshot,
    locked_replica_control_snapshot,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
INSTALL_STATE_ID = "01K22222222222222222222222"
USER_ID = "01K33333333333333333333333"
MANIFEST_DIGEST = "a" * 64
PROJECTION_SCOPE_ID = "b" * 64


def _binding(
    store_path: Path,
    *,
    mode: Literal["local", "cloud"] = "cloud",
) -> ResolvedProjectBinding:
    store_path.mkdir()
    return ResolvedProjectBinding(
        store_path=store_path,
        project_id=PROJECT_ID,
        display_name="Nauro",
        mode=mode,
        server_url="https://mcp.nauro.ai" if mode == "cloud" else None,
    )


def _marker() -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "authority": "generation",
            "project_id": PROJECT_ID,
            "store_format_version": 1,
        }
    ).encode()


def _pointer() -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": GENERATION_ID,
            "manifest_digest": MANIFEST_DIGEST,
            "committed_at": "2026-09-02T01:02:03.000004Z",
            "installed_at": "2026-09-02T01:03:04.000005Z",
            "installed_state_id": INSTALL_STATE_ID,
            "installed_for_user_id": USER_ID,
            "projection_class": "contributor_plus",
            "projection_scope_id": PROJECTION_SCOPE_ID,
        }
    ).encode()


def _write_control(layout: ReplicaControlLayout) -> tuple[bytes, bytes]:
    marker = _marker()
    pointer = _pointer()
    layout.authority_marker.parent.mkdir(parents=True)
    layout.authority_marker.write_bytes(marker)
    pointer_file = layout.actor_pointer(USER_ID)
    pointer_file.parent.mkdir(parents=True)
    pointer_file.write_bytes(pointer)
    return marker, pointer


def test_layout_uses_one_stable_lock_and_versioned_actor_pointer(tmp_path: Path) -> None:
    store = tmp_path / PROJECT_ID
    layout = ReplicaControlLayout(store)

    assert layout.control_lock == store / ".replica-control.lock"
    assert layout.control_root == store / ".replica"
    assert layout.authority_marker == store / ".replica" / "authority.json"
    assert layout.version_root == store / ".replica" / "v1"
    assert layout.actor_pointer(USER_ID) == (
        store / ".replica" / "v1" / "actors" / USER_ID / "pointer.json"
    )


@pytest.mark.parametrize("user_id", ["", "../actor", "user-1", PROJECT_ID.lower()])
def test_actor_pointer_rejects_noncanonical_user_id(tmp_path: Path, user_id: str) -> None:
    with pytest.raises(ValueError):
        ReplicaControlLayout(tmp_path).actor_pointer(user_id)


def test_local_project_does_not_touch_replica_control(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID, mode="local")
    layout = ReplicaControlLayout(binding.store_path)

    with locked_replica_control_snapshot(binding, active_user_id=None) as snapshot:
        assert snapshot == ReplicaControlSnapshot(marker_json=None, pointer_json=None)

    assert not layout.control_lock.exists()
    assert not layout.control_root.exists()


def test_cloud_legacy_snapshot_holds_lock_and_returns_absence(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)

    with locked_replica_control_snapshot(binding, active_user_id=None) as snapshot:
        assert snapshot == ReplicaControlSnapshot(marker_json=None, pointer_json=None)
        contender = FileLock(str(layout.control_lock))
        with pytest.raises(Timeout):
            contender.acquire(timeout=0)

    with FileLock(str(layout.control_lock), timeout=0):
        pass


def test_snapshot_reads_exact_marker_and_active_actor_pointer(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)
    marker, pointer = _write_control(layout)

    with locked_replica_control_snapshot(binding, active_user_id=USER_ID) as snapshot:
        assert snapshot == ReplicaControlSnapshot(marker_json=marker, pointer_json=pointer)
        with pytest.raises(Timeout):
            FileLock(str(layout.control_lock)).acquire(timeout=0)


def test_snapshot_bytes_feed_strict_authority_selection(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)
    _write_control(layout)

    with locked_replica_control_snapshot(binding, active_user_id=USER_ID) as snapshot:
        authority = select_project_authority(
            binding,
            marker_json=snapshot.marker_json,
            pointer_json=snapshot.pointer_json,
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )

    assert isinstance(authority, GenerationProjectAuthority)
    assert authority.pointer.generation_id == GENERATION_ID


def test_absent_marker_does_not_inspect_dormant_pointer(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)
    pointer = layout.actor_pointer(USER_ID)
    pointer.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("not-json")
    pointer.symlink_to(outside)

    with locked_replica_control_snapshot(binding, active_user_id=USER_ID) as snapshot:
        assert snapshot == ReplicaControlSnapshot(marker_json=None, pointer_json=None)


def test_invalid_active_user_does_not_form_or_read_pointer_path(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)
    layout.authority_marker.parent.mkdir(parents=True)
    marker = _marker()
    layout.authority_marker.write_bytes(marker)

    with locked_replica_control_snapshot(binding, active_user_id="../actor") as snapshot:
        assert snapshot == ReplicaControlSnapshot(marker_json=marker, pointer_json=None)


def test_active_marker_allows_missing_actor_pointer_snapshot(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)
    layout.authority_marker.parent.mkdir(parents=True)
    marker = _marker()
    layout.authority_marker.write_bytes(marker)

    with locked_replica_control_snapshot(binding, active_user_id=USER_ID) as snapshot:
        assert snapshot == ReplicaControlSnapshot(marker_json=marker, pointer_json=None)


@pytest.mark.parametrize("target", ["lock", "root", "marker", "actor", "pointer"])
def test_control_snapshot_refuses_symlink_components(tmp_path: Path, target: str) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)
    outside_file = tmp_path / "outside.json"
    outside_file.write_bytes(_marker())
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    if target == "lock":
        layout.control_lock.symlink_to(outside_file)
    elif target == "root":
        layout.control_root.symlink_to(outside_dir, target_is_directory=True)
    elif target == "marker":
        layout.control_root.mkdir()
        layout.authority_marker.symlink_to(outside_file)
    else:
        layout.authority_marker.parent.mkdir(parents=True)
        layout.authority_marker.write_bytes(_marker())
        pointer = layout.actor_pointer(USER_ID)
        pointer.parent.parent.mkdir(parents=True)
        if target == "actor":
            pointer.parent.symlink_to(outside_dir, target_is_directory=True)
        else:
            pointer.parent.mkdir()
            pointer.symlink_to(outside_file)

    with pytest.raises(ReplicaControlReadError) as raised:
        with locked_replica_control_snapshot(binding, active_user_id=USER_ID):
            pass

    assert raised.value.code == "generation_control_unavailable"


@pytest.mark.parametrize("kind", ["marker_directory", "marker_large", "pointer_large"])
def test_control_files_must_be_bounded_regular_files(tmp_path: Path, kind: str) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)
    layout.control_root.mkdir()
    if kind == "marker_directory":
        layout.authority_marker.mkdir()
    else:
        layout.authority_marker.write_bytes(
            b"x" * (16 * 1024 + 1) if kind == "marker_large" else _marker()
        )
        if kind == "pointer_large":
            pointer = layout.actor_pointer(USER_ID)
            pointer.parent.mkdir(parents=True)
            pointer.write_bytes(b"x" * (16 * 1024 + 1))

    with pytest.raises(ReplicaControlReadError):
        with locked_replica_control_snapshot(binding, active_user_id=USER_ID):
            pass


def test_busy_control_lock_has_typed_failure(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)
    layout = ReplicaControlLayout(binding.store_path)

    with FileLock(str(layout.control_lock)):
        with pytest.raises(ReplicaControlBusyError) as raised:
            with locked_replica_control_snapshot(binding, active_user_id=None, timeout=0):
                pass

    assert raised.value.code == "generation_control_busy"


def test_caller_exception_is_not_reclassified_as_control_failure(tmp_path: Path) -> None:
    binding = _binding(tmp_path / PROJECT_ID)

    with pytest.raises(OSError, match="caller failure"):
        with locked_replica_control_snapshot(binding, active_user_id=None):
            raise OSError("caller failure")


def test_control_snapshot_is_frozen() -> None:
    snapshot = ReplicaControlSnapshot(marker_json=b"{}", pointer_json=None)

    with pytest.raises(FrozenInstanceError):
        snapshot.pointer_json = b"{}"
