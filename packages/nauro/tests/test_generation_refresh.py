from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nauro.store.generation_authority import (
    GenerationProjectionIdentity,
    InstalledGenerationPointer,
    RefreshRequiredError,
)
from nauro.store.generation_installation import install_verified_generation
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.generation_refresh import (
    GenerationRefreshConflictError,
    GenerationRefreshError,
    PreparedGenerationRefresh,
    VerifiedGenerationRefresh,
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
GENERATION_THREE = "01K33333333333333333333333"
USER_ID = "01K44444444444444444444444"
SCOPE_ONE = "a" * 64
SCOPE_TWO = "b" * 64
COMMITTED_ONE = "2026-09-02T01:00:00.000000Z"
COMMITTED_TWO = "2026-09-02T02:00:00.000000Z"
COMMITTED_THREE = "2026-09-02T00:00:00.000000Z"


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
    committed_at: str,
    project_body: bytes,
) -> VerifiedGenerationProjection:
    artifacts = {
        "project.md": project_body,
        "decisions/001-first.md": b"# 1 - First\n",
    }
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
        committed_at=committed_at,
        installed_for_user_id=USER_ID,
        projection_class="contributor_plus",
        projection_scope_id=projection_scope_id,
    )
    return verify_generation_projection(
        GenerationProjectionTarget(binding=binding, identity=identity),
        manifest_json=manifest_json,
        artifacts=list(artifacts.items()),
    )


def _initial_projection(binding: ResolvedProjectBinding) -> VerifiedGenerationProjection:
    return _projection(
        binding,
        generation_id=GENERATION_ONE,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_ONE,
        project_body=b"# Initial\n",
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


def _verified_refresh(
    prepared: PreparedGenerationRefresh,
    projection: VerifiedGenerationProjection,
) -> VerifiedGenerationRefresh:
    return verify_generation_refresh(
        prepared,
        manifest_json=projection.manifest_json,
        artifacts=[(artifact.path, artifact.content) for artifact in projection.artifacts],
    )


def _installed_pointer(layout: ReplicaControlLayout) -> InstalledGenerationPointer:
    return InstalledGenerationPointer.model_validate_json(
        layout.actor_pointer(USER_ID).read_bytes()
    )


def test_refresh_replaces_the_exact_prepared_base(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _initial_projection(binding)
    layout = _activate(initial)
    target = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_TWO,
        project_body=b"# Refreshed\n",
    )

    prepared = prepare_generation_refresh(target.target)
    installed = install_generation_refresh(_verified_refresh(prepared, target))

    assert installed.generation_id == GENERATION_TWO
    assert _installed_pointer(layout) == installed
    with leased_generation_store(
        binding,
        active_user_id=USER_ID,
        active_projection_scope_id=SCOPE_ONE,
    ) as store:
        assert store.read_file("project.md") == "# Refreshed\n"


def test_competing_refresh_cannot_replace_a_changed_base(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _initial_projection(binding)
    layout = _activate(initial)
    second = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_TWO,
        project_body=b"# Second\n",
    )
    third = _projection(
        binding,
        generation_id=GENERATION_THREE,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_THREE,
        project_body=b"# Third\n",
    )
    second_refresh = _verified_refresh(prepare_generation_refresh(second.target), second)
    third_refresh = _verified_refresh(prepare_generation_refresh(third.target), third)

    installed = install_generation_refresh(third_refresh)
    with pytest.raises(GenerationRefreshConflictError) as raised:
        install_generation_refresh(second_refresh)

    assert raised.value.code == "generation_refresh_conflict"
    assert isinstance(raised.value, GenerationRefreshError)
    assert _installed_pointer(layout) == installed
    assert installed.generation_id == GENERATION_THREE
    assert not layout.generation_root(second.target.identity).exists()


def test_refresh_fence_rejects_pointer_aba(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _initial_projection(binding)
    layout = _activate(initial)
    second = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_TWO,
        project_body=b"# Second\n",
    )
    third = _projection(
        binding,
        generation_id=GENERATION_THREE,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_THREE,
        project_body=b"# Third\n",
    )
    stale = _verified_refresh(prepare_generation_refresh(second.target), second)

    install_generation_refresh(_verified_refresh(prepare_generation_refresh(third.target), third))
    returned = install_generation_refresh(
        _verified_refresh(prepare_generation_refresh(initial.target), initial)
    )

    assert returned.generation_id == GENERATION_ONE
    assert returned.installed_state_id != stale.prepared.base_pointer.installed_state_id
    with pytest.raises(GenerationRefreshConflictError):
        install_generation_refresh(stale)
    assert _installed_pointer(layout) == returned


def test_same_target_refreshes_converge_idempotently(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _initial_projection(binding)
    _activate(initial)
    target = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_TWO,
        project_body=b"# Refreshed\n",
    )
    first = _verified_refresh(prepare_generation_refresh(target.target), target)
    second = _verified_refresh(prepare_generation_refresh(target.target), target)

    first_pointer = install_generation_refresh(first)
    second_pointer = install_generation_refresh(second)

    assert second_pointer == first_pointer


def test_scope_refresh_does_not_wait_for_old_generation_readers(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _initial_projection(binding)
    _activate(initial)
    target = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_TWO,
        committed_at=COMMITTED_TWO,
        project_body=b"# New scope\n",
    )
    refresh = _verified_refresh(prepare_generation_refresh(target.target), target)

    with leased_generation_store(
        binding,
        active_user_id=USER_ID,
        active_projection_scope_id=SCOPE_ONE,
    ) as old_store:
        installed = install_generation_refresh(refresh)
        assert installed.projection_scope_id == SCOPE_TWO
        assert old_store.read_file("project.md") == "# Initial\n"

    with leased_generation_store(
        binding,
        active_user_id=USER_ID,
        active_projection_scope_id=SCOPE_TWO,
    ) as new_store:
        assert new_store.read_file("project.md") == "# New scope\n"


def test_refresh_capabilities_cannot_be_constructed_directly(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _initial_projection(binding)
    layout = _activate(initial)
    target = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_TWO,
        project_body=b"# Refreshed\n",
    )
    base = _installed_pointer(layout)

    with pytest.raises(GenerationRefreshError, match="lock-held"):
        PreparedGenerationRefresh(target.target, base, object())

    prepared = prepare_generation_refresh(target.target)
    with pytest.raises(GenerationRefreshError, match="do not match"):
        VerifiedGenerationRefresh(prepared, target, object())


def test_refresh_requires_active_generation_authority(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    initial = _initial_projection(binding)
    install_verified_generation(initial)
    target = _projection(
        binding,
        generation_id=GENERATION_TWO,
        projection_scope_id=SCOPE_ONE,
        committed_at=COMMITTED_TWO,
        project_body=b"# Refreshed\n",
    )

    with pytest.raises(RefreshRequiredError, match="has not selected"):
        prepare_generation_refresh(target.target)
