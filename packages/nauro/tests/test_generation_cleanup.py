from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import nauro.store.generation_cleanup as generation_cleanup
from nauro.store.generation_authority import GenerationProjectionIdentity
from nauro.store.generation_cleanup import (
    GenerationCleanupError,
    cleanup_actor_generations,
)
from nauro.store.generation_installation import install_verified_generation
from nauro.store.generation_lease import generation_read_lease
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.generation_refresh import (
    install_generation_refresh,
    prepare_generation_refresh,
    verify_generation_refresh,
)
from nauro.store.generation_store import leased_generation_store
from nauro.store.replica_control import ReplicaControlLayout
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ONE = "01K11111111111111111111111"
GENERATION_TWO = "01K22222222222222222222222"
USER_ID = "01K33333333333333333333333"
CLEANUP_STATE_ID = "01K44444444444444444444444"
SCOPE_ONE = "a" * 64
SCOPE_TWO = "b" * 64


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
    binding: ResolvedProjectBinding,
    *,
    generation_id: str,
    projection_scope_id: str,
    body: bytes,
) -> VerifiedGenerationProjection:
    artifacts = {"project.md": body}
    manifest_json = json.dumps(
        {
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": generation_id,
            "projection_class": "contributor_plus",
            "projection_scope_id": projection_scope_id,
            "artifacts": {
                path: hashlib.sha256(content).hexdigest() for path, content in artifacts.items()
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    identity = GenerationProjectionIdentity(
        project_id=PROJECT_ID,
        store_format_version=1,
        generation_id=generation_id,
        manifest_digest=hashlib.sha256(manifest_json).hexdigest(),
        committed_at="2026-09-02T01:00:00.000000Z",
        installed_for_user_id=USER_ID,
        projection_class="contributor_plus",
        projection_scope_id=projection_scope_id,
    )
    return verify_generation_projection(
        GenerationProjectionTarget(binding=binding, identity=identity),
        manifest_json=manifest_json,
        artifacts=list(artifacts.items()),
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


def _refresh(projection: VerifiedGenerationProjection) -> None:
    prepared = prepare_generation_refresh(projection.target)
    verified = verify_generation_refresh(
        prepared,
        manifest_json=projection.manifest_json,
        artifacts=[(artifact.path, artifact.content) for artifact in projection.artifacts],
    )
    install_generation_refresh(verified)


def _two_generations(
    tmp_path: Path,
    *,
    current_generation_id: str = GENERATION_TWO,
    current_scope_id: str = SCOPE_ONE,
) -> tuple[
    ResolvedProjectBinding,
    ReplicaControlLayout,
    VerifiedGenerationProjection,
    VerifiedGenerationProjection,
]:
    binding = _binding(tmp_path)
    initial = _projection(
        binding,
        generation_id=GENERATION_ONE,
        projection_scope_id=SCOPE_ONE,
        body=b"# Initial\n",
    )
    layout = _activate(initial)
    current = _projection(
        binding,
        generation_id=current_generation_id,
        projection_scope_id=current_scope_id,
        body=b"# Current\n",
    )
    _refresh(current)
    return binding, layout, initial, current


def _generation_label(projection: VerifiedGenerationProjection) -> str:
    identity = projection.target.identity
    return (
        f"{identity.installed_for_user_id}/{identity.projection_scope_id}/{identity.generation_id}"
    )


def _tombstone_path(
    layout: ReplicaControlLayout,
    projection: VerifiedGenerationProjection,
) -> Path:
    identity = projection.target.identity
    return (
        layout.projection_root(identity) / "tombstones" / identity.generation_id / CLEANUP_STATE_ID
    )


def test_cleanup_deletes_only_unpointed_generation_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, layout, initial, current = _two_generations(tmp_path)
    monkeypatch.setattr(generation_cleanup, "generate_ulid", lambda: CLEANUP_STATE_ID)

    report = cleanup_actor_generations(binding, active_user_id=USER_ID)

    assert report.deleted_generations == (_generation_label(initial),)
    assert report.recovered_tombstones == ()
    assert report.busy == ()
    assert not layout.generation_root(initial.target.identity).exists()
    assert layout.generation_root(current.target.identity).is_dir()
    assert layout.generation_manifest(initial.target.identity).is_file()
    assert layout.generation_lease(initial.target.identity).is_file()
    assert not _tombstone_path(layout, initial).exists()


def test_cleanup_skips_generation_with_live_reader(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _projection(
        binding,
        generation_id=GENERATION_ONE,
        projection_scope_id=SCOPE_ONE,
        body=b"# Initial\n",
    )
    layout = _activate(initial)
    current = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_ONE,
        body=b"# Current\n",
    )

    with leased_generation_store(
        binding,
        active_user_id=USER_ID,
        active_projection_scope_id=SCOPE_ONE,
    ) as old_store:
        _refresh(current)
        report = cleanup_actor_generations(binding, active_user_id=USER_ID)
        assert report.deleted_generations == ()
        assert report.busy == (f"generation:{_generation_label(initial)}",)
        assert old_store.read_file("project.md") == "# Initial\n"
        assert layout.generation_root(initial.target.identity).is_dir()

    report = cleanup_actor_generations(binding, active_user_id=USER_ID)
    assert report.deleted_generations == (_generation_label(initial),)


def test_cleanup_recovers_interrupted_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, layout, initial, current = _two_generations(tmp_path)
    monkeypatch.setattr(generation_cleanup, "generate_ulid", lambda: CLEANUP_STATE_ID)
    original_delete = generation_cleanup._delete_tombstone

    def interrupt_delete(layout: ReplicaControlLayout, path: Path) -> None:
        raise RuntimeError("process interrupted")

    monkeypatch.setattr(generation_cleanup, "_delete_tombstone", interrupt_delete)
    with pytest.raises(RuntimeError, match="interrupted"):
        cleanup_actor_generations(binding, active_user_id=USER_ID)

    tombstone = _tombstone_path(layout, initial)
    assert not layout.generation_root(initial.target.identity).exists()
    assert tombstone.is_dir()

    monkeypatch.setattr(generation_cleanup, "_delete_tombstone", original_delete)
    with generation_read_lease(layout, initial.target.identity):
        busy = cleanup_actor_generations(binding, active_user_id=USER_ID)
        assert busy.recovered_tombstones == ()
        assert busy.busy == (f"tombstone:{_generation_label(initial)}/{CLEANUP_STATE_ID}",)
        assert tombstone.is_dir()

    report = cleanup_actor_generations(binding, active_user_id=USER_ID)

    assert report.recovered_tombstones == (f"{_generation_label(initial)}/{CLEANUP_STATE_ID}",)
    assert report.deleted_generations == ()
    assert not tombstone.exists()
    assert layout.generation_root(current.target.identity).is_dir()


def test_cleanup_validates_full_inventory_before_deletion(tmp_path: Path) -> None:
    binding, layout, initial, _ = _two_generations(tmp_path)
    malformed = layout.generation_root(initial.target.identity).parent / "not-a-generation"
    malformed.mkdir()

    with pytest.raises(GenerationCleanupError, match="invalid generation_id"):
        cleanup_actor_generations(binding, active_user_id=USER_ID)

    assert layout.generation_root(initial.target.identity).is_dir()


def test_cleanup_refuses_link_inside_candidate_root(tmp_path: Path) -> None:
    binding, layout, initial, _ = _two_generations(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("retain")
    link = layout.generation_root(initial.target.identity) / "linked"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("platform does not permit test symlinks")

    with pytest.raises(GenerationCleanupError, match="unsafe"):
        cleanup_actor_generations(binding, active_user_id=USER_ID)

    assert layout.generation_root(initial.target.identity).is_dir()
    assert outside.read_text() == "retain"


def test_cleanup_identity_includes_projection_scope(tmp_path: Path) -> None:
    binding, layout, initial, current = _two_generations(
        tmp_path,
        current_generation_id=GENERATION_ONE,
        current_scope_id=SCOPE_TWO,
    )

    report = cleanup_actor_generations(binding, active_user_id=USER_ID)

    assert report.deleted_generations == (_generation_label(initial),)
    assert layout.generation_root(current.target.identity).is_dir()


def test_cleanup_retains_the_only_pointed_generation(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    current = _projection(
        binding,
        generation_id=GENERATION_ONE,
        projection_scope_id=SCOPE_ONE,
        body=b"# Current\n",
    )
    layout = _activate(current)

    report = cleanup_actor_generations(binding, active_user_id=USER_ID)

    assert report == generation_cleanup.GenerationCleanupReport((), (), ())
    assert layout.generation_root(current.target.identity).is_dir()
