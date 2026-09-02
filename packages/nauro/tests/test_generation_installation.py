from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

import nauro.store.generation_installation as installation
from nauro.store.generation_authority import (
    GenerationControlCorruptError,
    GenerationProjectionIdentity,
    InstalledGenerationPointer,
    select_project_authority,
)
from nauro.store.generation_installation import (
    GenerationInstallationError,
    install_verified_generation,
)
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.replica_control import (
    ReplicaControlLayout,
    ReplicaControlReadError,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
OTHER_GENERATION_ID = "01K22222222222222222222222"
INSTALL_STATE_ID = "01K33333333333333333333333"
OTHER_INSTALL_STATE_ID = "01K44444444444444444444444"
USER_ID = "01K55555555555555555555555"
PROJECTION_SCOPE_ID = "a" * 64
OTHER_PROJECTION_SCOPE_ID = "b" * 64
COMMITTED_AT = "2026-09-02T01:02:03.000004Z"
NEWER_COMMITTED_AT = "2026-09-02T02:02:03.000004Z"
INSTALLED_AT = "2026-09-02T03:02:03.000004Z"


@pytest.fixture(autouse=True)
def _fixed_install_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installation, "_new_install_state_id", lambda: INSTALL_STATE_ID)
    monkeypatch.setattr(installation, "_installed_at", lambda: INSTALLED_AT)


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
    binding: ResolvedProjectBinding | None = None,
    generation_id: str = GENERATION_ID,
    projection_scope_id: str = PROJECTION_SCOPE_ID,
    committed_at: str = COMMITTED_AT,
    contents: dict[str, bytes] | None = None,
) -> VerifiedGenerationProjection:
    bound = binding or _binding(tmp_path)
    artifact_bytes = contents or {
        "project.md": b"# Project\n",
        "decisions/001-use-postgres.md": b"# 1 - Use Postgres\n",
    }
    manifest_json = json.dumps(
        {
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": generation_id,
            "projection_class": "contributor_plus",
            "projection_scope_id": projection_scope_id,
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
        generation_id=generation_id,
        manifest_digest=hashlib.sha256(manifest_json).hexdigest(),
        committed_at=committed_at,
        installed_for_user_id=USER_ID,
        projection_class="contributor_plus",
        projection_scope_id=projection_scope_id,
    )
    return verify_generation_projection(
        GenerationProjectionTarget(binding=bound, identity=identity),
        manifest_json=manifest_json,
        artifacts=list(artifact_bytes.items()),
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


def _activate_marker(layout: ReplicaControlLayout) -> None:
    layout.authority_marker.parent.mkdir(parents=True, exist_ok=True)
    layout.authority_marker.write_bytes(_marker())


def test_layout_and_install_keep_control_outside_generation_root(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    identity = projection.target.identity

    pointer = install_verified_generation(projection)

    projection_root = layout.version_root / "actors" / USER_ID / "projections" / PROJECTION_SCOPE_ID
    assert layout.projection_root(identity) == projection_root
    assert layout.generation_root(identity) == projection_root / "generations" / GENERATION_ID
    assert layout.generation_manifest(identity) == (
        projection_root / "manifests" / f"{GENERATION_ID}.json"
    )
    assert layout.generation_lease(identity) == (
        projection_root / "leases" / f"{GENERATION_ID}.lock"
    )
    root = layout.generation_root(identity)
    assert (root / "project.md").read_bytes() == b"# Project\n"
    assert (root / "decisions" / "001-use-postgres.md").is_file()
    assert layout.generation_manifest(identity).read_bytes() == projection.manifest_json
    assert layout.generation_lease(identity).read_bytes() == b""
    assert not layout.authority_marker.exists()
    assert not layout.staging_root(identity, INSTALL_STATE_ID).exists()
    assert pointer.installed_state_id == INSTALL_STATE_ID
    assert pointer.installed_at == INSTALLED_AT
    stored = InstalledGenerationPointer.model_validate_json(
        layout.actor_pointer(USER_ID).read_bytes()
    )
    assert stored == pointer
    assert (
        select_project_authority(
            projection.target.binding,
            marker_json=None,
            pointer_json=layout.actor_pointer(USER_ID).read_bytes(),
        ).kind
        == "hosted_legacy"
    )


def test_pointer_is_written_last_under_the_control_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    pointer_path = layout.actor_pointer(USER_ID)
    writes: list[Path] = []
    original = installation.atomic_write_bytes

    def observed_write(path: Path, content: bytes) -> None:
        writes.append(path)
        if path == pointer_path:
            with pytest.raises(Timeout):
                FileLock(str(layout.control_lock)).acquire(timeout=0)
        original(path, content)

    monkeypatch.setattr(installation, "atomic_write_bytes", observed_write)

    install_verified_generation(projection)

    assert writes[-1] == pointer_path


def test_pointer_failure_leaves_reusable_unpointed_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    pointer_path = layout.actor_pointer(USER_ID)
    original = installation.atomic_write_bytes

    def fail_pointer(path: Path, content: bytes) -> None:
        if path == pointer_path:
            raise OSError("pointer failure")
        original(path, content)

    monkeypatch.setattr(installation, "atomic_write_bytes", fail_pointer)
    with pytest.raises(GenerationInstallationError, match="pointer could not"):
        install_verified_generation(projection)

    assert layout.generation_root(projection.target.identity).is_dir()
    assert not pointer_path.exists()
    monkeypatch.setattr(installation, "atomic_write_bytes", original)
    assert install_verified_generation(projection).generation_id == GENERATION_ID


def test_staged_bytes_are_reverified_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    original = installation.atomic_write_bytes

    def corrupt_artifact(path: Path, content: bytes) -> None:
        original(path, b"corrupt" if path.name == "project.md" else content)

    monkeypatch.setattr(installation, "atomic_write_bytes", corrupt_artifact)
    with pytest.raises(GenerationInstallationError, match="artifact diverges"):
        install_verified_generation(projection)

    assert not layout.generation_root(projection.target.identity).exists()
    assert not layout.actor_pointer(USER_ID).exists()
    assert not layout.staging_root(projection.target.identity, INSTALL_STATE_ID).exists()


@pytest.mark.parametrize("defect", ["artifact", "extra", "manifest", "lease"])
def test_existing_generation_material_is_immutable(tmp_path: Path, defect: str) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    install_verified_generation(projection)
    identity = projection.target.identity
    if defect == "artifact":
        (layout.generation_root(identity) / "project.md").write_bytes(b"changed")
    elif defect == "extra":
        (layout.generation_root(identity) / "extra.md").write_bytes(b"extra")
    elif defect == "manifest":
        layout.generation_manifest(identity).write_bytes(b"{}")
    else:
        layout.generation_lease(identity).write_bytes(b"not-empty")

    with pytest.raises(GenerationInstallationError):
        install_verified_generation(projection)


def test_corrupt_marker_blocks_install_before_pointer_publication(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    layout.authority_marker.parent.mkdir(parents=True)
    layout.authority_marker.write_bytes(b"not-json")

    with pytest.raises(GenerationControlCorruptError):
        install_verified_generation(projection)

    assert not layout.generation_root(projection.target.identity).exists()
    assert not layout.actor_pointer(USER_ID).exists()


def test_absent_marker_allows_replacing_dormant_pointer(tmp_path: Path) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    pointer_path = layout.actor_pointer(USER_ID)
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_bytes(b"dormant-invalid")

    installed = install_verified_generation(projection)

    assert InstalledGenerationPointer.model_validate_json(pointer_path.read_bytes()) == installed


@pytest.mark.parametrize(
    "changes",
    [
        {"generation_id": OTHER_GENERATION_ID, "committed_at": NEWER_COMMITTED_AT},
        {"projection_scope_id": OTHER_PROJECTION_SCOPE_ID},
    ],
)
def test_active_pointer_refuses_unproved_replacement(
    tmp_path: Path,
    changes: dict[str, str],
) -> None:
    binding = _binding(tmp_path)
    current = _projection(
        tmp_path,
        binding=binding,
        generation_id=GENERATION_ID,
        committed_at=COMMITTED_AT,
    )
    layout = ReplicaControlLayout(binding.store_path)
    _activate_marker(layout)
    install_verified_generation(current)
    target = _projection(
        tmp_path,
        binding=binding,
        **changes,
    )

    with pytest.raises(GenerationInstallationError, match="refresh commit"):
        install_verified_generation(target)

    stored = InstalledGenerationPointer.model_validate_json(
        layout.actor_pointer(USER_ID).read_bytes()
    )
    assert stored.generation_id == GENERATION_ID
    assert stored.projection_scope_id == PROJECTION_SCOPE_ID
    assert not layout.generation_root(target.target.identity).exists()


def test_same_active_identity_reuses_installed_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    _activate_marker(layout)
    first = install_verified_generation(projection)
    monkeypatch.setattr(
        installation,
        "_new_install_state_id",
        lambda: OTHER_INSTALL_STATE_ID,
    )

    second = install_verified_generation(projection)

    assert second == first
    assert not layout.staging_root(projection.target.identity, OTHER_INSTALL_STATE_ID).exists()


def test_local_clock_skew_does_not_invalidate_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path, committed_at=NEWER_COMMITTED_AT)
    monkeypatch.setattr(
        installation,
        "_installed_at",
        lambda: "2026-09-02T01:00:00.000000Z",
    )

    pointer = install_verified_generation(projection)

    assert pointer.installed_at < pointer.committed_at


@pytest.mark.parametrize("target_name", ["staging", "root", "manifest", "lease", "pointer"])
def test_install_refuses_symlinked_control_paths(tmp_path: Path, target_name: str) -> None:
    projection = _projection(tmp_path)
    layout = ReplicaControlLayout(projection.target.binding.store_path)
    identity = projection.target.identity
    targets = {
        "staging": layout.staging_root(identity, INSTALL_STATE_ID),
        "root": layout.generation_root(identity),
        "manifest": layout.generation_manifest(identity),
        "lease": layout.generation_lease(identity),
        "pointer": layout.actor_pointer(USER_ID),
    }
    target = targets[target_name]
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / ("outside-dir" if target_name in ("staging", "root") else "outside")
    if target_name in ("staging", "root"):
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    else:
        outside.write_bytes(b"outside")
        target.symlink_to(outside)

    with pytest.raises(ReplicaControlReadError):
        install_verified_generation(projection)
