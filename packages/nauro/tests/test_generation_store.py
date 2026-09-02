from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from filelock import FileLock, Timeout
from nauro_core.operations.store import Store

import nauro.store.generation_store as generation_store
from nauro.store.generation_authority import GenerationProjectionIdentity
from nauro.store.generation_installation import install_verified_generation
from nauro.store.generation_lease import (
    GenerationLeaseBusyError,
    generation_cleanup_lease,
)
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.generation_store import (
    GenerationStoreReadOnlyError,
    GenerationStoreUnavailableError,
    LeasedGenerationStore,
    leased_generation_store,
)
from nauro.store.replica_control import ReplicaControlLayout
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K22222222222222222222222"
OTHER_INSTALL_STATE_ID = "01K33333333333333333333333"
PROJECTION_SCOPE_ID = "a" * 64
COMMITTED_AT = "2026-09-02T01:02:03.000004Z"


def _binding(tmp_path: Path) -> ResolvedProjectBinding:
    store = tmp_path / PROJECT_ID
    store.mkdir()
    return ResolvedProjectBinding(
        store_path=store,
        project_id=PROJECT_ID,
        display_name="Nauro",
        mode="cloud",
        server_url="https://mcp.nauro.ai",
    )


def _projection(
    tmp_path: Path,
    *,
    contents: dict[str, bytes] | None = None,
) -> VerifiedGenerationProjection:
    binding = _binding(tmp_path)
    artifact_bytes = contents or {
        "project.md": b"# Project\n",
        "decisions/002-second.md": b"# 2 - Second\n",
        "decisions/001-first.md": b"# 1 - First\n",
    }
    manifest_json = json.dumps(
        {
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": GENERATION_ID,
            "projection_class": "contributor_plus",
            "projection_scope_id": PROJECTION_SCOPE_ID,
            "artifacts": {
                path: hashlib.sha256(content).hexdigest()
                for path, content in artifact_bytes.items()
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    identity = GenerationProjectionIdentity(
        project_id=PROJECT_ID,
        store_format_version=1,
        generation_id=GENERATION_ID,
        manifest_digest=hashlib.sha256(manifest_json).hexdigest(),
        committed_at=COMMITTED_AT,
        installed_for_user_id=USER_ID,
        projection_class="contributor_plus",
        projection_scope_id=PROJECTION_SCOPE_ID,
    )
    return verify_generation_projection(
        GenerationProjectionTarget(binding=binding, identity=identity),
        manifest_json=manifest_json,
        artifacts=list(artifact_bytes.items()),
    )


def _activate(projection: VerifiedGenerationProjection) -> ReplicaControlLayout:
    install_verified_generation(projection)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    layout.authority_marker.parent.mkdir(parents=True, exist_ok=True)
    layout.authority_marker.write_bytes(
        json.dumps(
            {
                "schema_version": 1,
                "authority": "generation",
                "project_id": PROJECT_ID,
                "store_format_version": 1,
            }
        ).encode()
    )
    return layout


def _open_store(projection: VerifiedGenerationProjection):
    return leased_generation_store(
        projection.target.binding,
        active_user_id=USER_ID,
        active_projection_scope_id=PROJECTION_SCOPE_ID,
    )


def test_store_holds_only_the_generation_lease_for_its_lifetime(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = _activate(projection)
    retained = None

    with _open_store(projection) as store:
        retained = store
        assert isinstance(store, Store)
        assert store.read_file("project.md") == "# Project\n"
        assert store.list_decisions() == ["001-first", "002-second"]
        assert store.read_decisions(["002-second", "001-first"]) == {
            "002-second": "# 2 - Second\n",
            "001-first": "# 1 - First\n",
        }
        with pytest.raises(FrozenInstanceError):
            store._root = tmp_path
        with FileLock(str(layout.control_lock), timeout=0):
            pass
        with pytest.raises(GenerationLeaseBusyError):
            with generation_cleanup_lease(layout, projection.target.identity):
                pass

    assert retained is not None
    with pytest.raises(GenerationStoreUnavailableError, match="lease has ended"):
        retained.read_file("project.md")
    with generation_cleanup_lease(layout, projection.target.identity):
        pass


def test_store_is_read_only_and_rejects_nonprotected_paths(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = _activate(projection)
    project = layout.generation_root(projection.target.identity) / "project.md"

    with _open_store(projection) as store:
        assert store.read_file("stack.md") is None
        with pytest.raises(GenerationStoreUnavailableError, match="outside"):
            store.read_file("snapshots/v001.json")
        with pytest.raises(GenerationStoreReadOnlyError) as write_error:
            store.write_file("project.md", "changed")
        with pytest.raises(GenerationStoreReadOnlyError):
            store.delete_file("project.md")

    assert write_error.value.code == "generation_store_read_only"
    assert project.read_bytes() == b"# Project\n"


def test_store_cannot_be_constructed_without_a_lifetime_lease(tmp_path: Path) -> None:
    projection = _projection(tmp_path)

    with pytest.raises(GenerationStoreUnavailableError, match="lifetime-lease context"):
        LeasedGenerationStore(
            projection.target.binding.store_path,
            projection.manifest,
            object(),
        )


def test_manifest_tampering_blocks_store_creation(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = _activate(projection)
    layout.generation_manifest(projection.target.identity).write_bytes(b"{}")

    with pytest.raises(GenerationStoreUnavailableError, match="manifest digest"):
        with _open_store(projection):
            pass


def test_artifact_tampering_fails_at_the_read_boundary(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = _activate(projection)
    root = layout.generation_root(projection.target.identity)
    (root / "project.md").write_bytes(b"changed")

    with _open_store(projection) as store:
        with pytest.raises(GenerationStoreUnavailableError, match="digest diverges"):
            store.read_file("project.md")


def test_unmanifested_material_blocks_store_creation(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = _activate(projection)
    root = layout.generation_root(projection.target.identity)
    (root / "extra.md").write_bytes(b"extra")

    with pytest.raises(GenerationStoreUnavailableError, match="unexpected file"):
        with _open_store(projection):
            pass


def test_symlinked_artifact_blocks_store_creation(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = _activate(projection)
    root = layout.generation_root(projection.target.identity)
    project = root / "project.md"
    project.unlink()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"# Project\n")
    try:
        project.symlink_to(outside)
    except OSError:
        pytest.skip("platform does not permit test symlinks")

    with pytest.raises(GenerationStoreUnavailableError, match="link or reparse"):
        with _open_store(projection):
            pass


def test_pointer_change_after_lease_acquisition_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path)
    layout = _activate(projection)
    original = generation_store.generation_read_lease

    @contextmanager
    def mutate_pointer(
        active_layout: ReplicaControlLayout,
        identity: GenerationProjectionIdentity,
    ):
        with original(active_layout, identity):
            with pytest.raises(Timeout):
                FileLock(str(active_layout.control_lock)).acquire(timeout=0)
            pointer_path = active_layout.actor_pointer(USER_ID)
            pointer = json.loads(pointer_path.read_bytes())
            pointer["installed_state_id"] = OTHER_INSTALL_STATE_ID
            pointer_path.write_bytes(json.dumps(pointer).encode())
            yield

    monkeypatch.setattr(generation_store, "generation_read_lease", mutate_pointer)

    with pytest.raises(GenerationStoreUnavailableError, match="pointer changed"):
        with _open_store(projection):
            pass

    with generation_cleanup_lease(layout, projection.target.identity):
        pass


def test_absent_authority_marker_does_not_open_generation_store(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    install_verified_generation(projection)

    with pytest.raises(GenerationStoreUnavailableError, match="has not selected"):
        with _open_store(projection):
            pass
