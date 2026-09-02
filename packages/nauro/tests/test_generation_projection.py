from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pydantic as pyd
import pytest

from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    GenerationProjectionIdentity,
)
from nauro.store.generation_projection import (
    GenerationProjectionManifest,
    GenerationProjectionTarget,
    GenerationProjectionVerificationError,
    VerifiedGenerationArtifact,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
OTHER_PROJECT_ID = "01K00000000000000000000000"
GENERATION_ID = "01K11111111111111111111111"
OTHER_GENERATION_ID = "01K22222222222222222222222"
USER_ID = "01K44444444444444444444444"
PROJECTION_SCOPE_ID = "a" * 64


def _manifest_object(
    *,
    project_id: object = PROJECT_ID,
    store_format_version: object = 1,
    generation_id: object = GENERATION_ID,
    projection_class: object = "contributor_plus",
    projection_scope_id: object = PROJECTION_SCOPE_ID,
    artifacts: object | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "project_id": project_id,
        "store_format_version": store_format_version,
        "generation_id": generation_id,
        "projection_class": projection_class,
        "projection_scope_id": projection_scope_id,
        "artifacts": (
            {"project.md": hashlib.sha256(b"# Project\n").hexdigest()}
            if artifacts is None
            else artifacts
        ),
    }
    if extra:
        body.update(extra)
    return body


def _canonical_manifest(**changes: object) -> bytes:
    return json.dumps(
        _manifest_object(**changes),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _target(
    manifest_json: bytes,
    *,
    binding: ResolvedProjectBinding | None = None,
    **changes: object,
) -> GenerationProjectionTarget:
    if binding is None:
        binding = ResolvedProjectBinding(
            store_path=Path("store"),
            project_id=PROJECT_ID,
            display_name="Nauro",
            mode="cloud",
            server_url="https://mcp.nauro.ai",
        )
    values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "store_format_version": 1,
        "generation_id": GENERATION_ID,
        "manifest_digest": hashlib.sha256(manifest_json).hexdigest(),
        "committed_at": "2026-09-02T01:02:03.000004Z",
        "installed_for_user_id": USER_ID,
        "projection_class": "contributor_plus",
        "projection_scope_id": PROJECTION_SCOPE_ID,
    }
    values.update(changes)
    identity = GenerationProjectionIdentity(**values)  # type: ignore[arg-type]
    return GenerationProjectionTarget(binding=binding, identity=identity)


def test_projection_target_binds_validated_cloud_and_server_facts() -> None:
    manifest_json = _canonical_manifest()

    target = _target(manifest_json)

    assert target.binding.project_id == PROJECT_ID
    assert target.identity.store_format_version == 1
    assert target.identity.generation_id == GENERATION_ID
    assert target.identity.manifest_digest == hashlib.sha256(manifest_json).hexdigest()
    assert target.identity.installed_for_user_id == USER_ID
    assert target.identity.projection_scope_id == PROJECTION_SCOPE_ID
    with pytest.raises(FrozenInstanceError):
        target.identity = target.identity
    with pytest.raises(pyd.ValidationError):
        target.identity.generation_id = OTHER_GENERATION_ID


def test_projection_target_rejects_unsupported_store_format() -> None:
    with pytest.raises(ClientUpgradeRequiredError):
        _target(_canonical_manifest(), store_format_version=2)


@pytest.mark.parametrize(
    "changes",
    [
        {"generation_id": "generation-1"},
        {"manifest_digest": "A" * 64},
        {"manifest_digest": "a" * 63},
        {"committed_at": "2026-09-02T01:02:03Z"},
        {"installed_for_user_id": "user-1"},
        {"projection_class": "owner"},
        {"projection_scope_id": "A" * 64},
        {"projection_scope_id": "a" * 65},
        {"generation_id": False},
        {"store_format_version": False},
        {"store_format_version": 0},
    ],
)
def test_projection_identity_rejects_malformed_facts(changes: dict[str, object]) -> None:
    with pytest.raises(pyd.ValidationError):
        _target(_canonical_manifest(), **changes)


def test_projection_target_cross_binds_project_identity() -> None:
    with pytest.raises(GenerationProjectionVerificationError, match="another project"):
        _target(_canonical_manifest(), project_id=OTHER_PROJECT_ID)


def test_projection_target_requires_cloud_binding() -> None:
    binding = ResolvedProjectBinding(
        store_path=Path("store"),
        project_id=PROJECT_ID,
        display_name="Nauro",
        mode="local",
        server_url=None,
    )

    with pytest.raises(GenerationProjectionVerificationError, match="cloud binding"):
        _target(_canonical_manifest(), binding=binding)


def test_verifies_exact_projection_and_sorts_artifacts() -> None:
    project = b"# Project\n"
    state = b"# State\n"
    manifest_json = _canonical_manifest(
        artifacts={
            "state_current.md": hashlib.sha256(state).hexdigest(),
            "project.md": hashlib.sha256(project).hexdigest(),
        }
    )

    projection = verify_generation_projection(
        _target(manifest_json),
        manifest_json=manifest_json,
        artifacts=[("state_current.md", state), ("project.md", project)],
    )

    assert projection.project_id == PROJECT_ID
    assert projection.generation_id == GENERATION_ID
    assert projection.manifest_digest == hashlib.sha256(manifest_json).hexdigest()
    assert projection.manifest.canonical_bytes() == manifest_json
    assert [artifact.path for artifact in projection.artifacts] == [
        "project.md",
        "state_current.md",
    ]
    assert projection.total_bytes == len(project) + len(state)


def test_empty_born_current_projection_is_valid() -> None:
    manifest_json = _canonical_manifest(artifacts={})

    projection = verify_generation_projection(
        _target(manifest_json),
        manifest_json=manifest_json,
        artifacts=[],
    )

    assert projection.artifacts == ()
    assert projection.total_bytes == 0


def test_manifest_digest_must_match_committed_pointer() -> None:
    expected = _canonical_manifest()
    divergent = expected.replace(b"project.md", b"state.md")

    with pytest.raises(GenerationProjectionVerificationError, match="digest diverges"):
        verify_generation_projection(
            _target(expected),
            manifest_json=divergent,
            artifacts=[],
        )


@pytest.mark.parametrize(
    "manifest_json",
    [
        json.dumps(_manifest_object(), sort_keys=False).encode(),
        json.dumps(_manifest_object(), sort_keys=True, indent=2).encode(),
        _canonical_manifest() + b"\n",
    ],
)
def test_manifest_bytes_must_use_canonical_json(manifest_json: bytes) -> None:
    with pytest.raises(GenerationProjectionVerificationError, match="not canonical"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[("project.md", b"# Project\n")],
        )


def test_manifest_rejects_duplicate_json_keys() -> None:
    canonical = _canonical_manifest().decode()
    duplicate = canonical.replace(
        f'"generation_id":"{GENERATION_ID}"',
        f'"generation_id":"{OTHER_GENERATION_ID}","generation_id":"{GENERATION_ID}"',
    ).encode()

    with pytest.raises(GenerationProjectionVerificationError, match="malformed"):
        verify_generation_projection(
            _target(duplicate),
            manifest_json=duplicate,
            artifacts=[("project.md", b"# Project\n")],
        )


def test_manifest_rejects_unencodable_json_strings() -> None:
    body = _manifest_object(artifacts={"decisions/\ud800.md": "b" * 64})
    manifest_json = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(GenerationProjectionVerificationError, match="malformed"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[],
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"generation_id": False},
        {"generation_id": GENERATION_ID.lower()},
        {"project_id": False},
        {"store_format_version": False},
        {"store_format_version": 0},
        {"projection_class": False},
        {"projection_class": "owner"},
        {"projection_scope_id": False},
        {"projection_scope_id": "A" * 64},
        {"projection_scope_id": "a" * 65},
        {"artifacts": []},
        {"artifacts": {"project.md": "B" * 64}},
        {"artifacts": {"project.md": "b" * 63}},
        {"extra": {"schema_version": 1}},
    ],
)
def test_manifest_fields_are_strict_and_closed(changes: dict[str, object]) -> None:
    manifest_json = _canonical_manifest(**changes)

    with pytest.raises(GenerationProjectionVerificationError, match="malformed"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[],
        )


@pytest.mark.parametrize(
    "artifacts",
    [
        {"../project.md": "b" * 64},
        {"unknown.md": "b" * 64},
        {"decisions/nested/001-bad.md": "b" * 64},
    ],
)
def test_manifest_paths_use_shared_protected_membership(
    artifacts: dict[str, str],
) -> None:
    manifest_json = _canonical_manifest(artifacts=artifacts)

    with pytest.raises(GenerationProjectionVerificationError, match="malformed"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[],
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": OTHER_PROJECT_ID},
        {"store_format_version": 2},
        {"generation_id": OTHER_GENERATION_ID},
        {"projection_class": "viewer"},
        {"projection_scope_id": "b" * 64},
    ],
)
def test_manifest_must_cross_bind_to_installed_projection(
    changes: dict[str, object],
) -> None:
    manifest_json = _canonical_manifest(**changes)

    with pytest.raises(GenerationProjectionVerificationError, match="projection binding"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[("project.md", b"# Project\n")],
        )


@pytest.mark.parametrize(
    "artifacts",
    [
        [],
        [("project.md", b"# Project\n"), ("state.md", b"# State\n")],
    ],
)
def test_downloaded_artifact_set_must_equal_manifest(
    artifacts: list[tuple[str, bytes]],
) -> None:
    manifest_json = _canonical_manifest()

    with pytest.raises(GenerationProjectionVerificationError, match="differs"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=artifacts,
        )


def test_downloaded_artifact_paths_must_be_unique() -> None:
    manifest_json = _canonical_manifest()

    with pytest.raises(GenerationProjectionVerificationError, match="duplicate"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[
                ("project.md", b"# Project\n"),
                ("project.md", b"# Project\n"),
            ],
        )


def test_downloaded_artifact_digest_must_match_manifest() -> None:
    manifest_json = _canonical_manifest()

    with pytest.raises(GenerationProjectionVerificationError, match="project.md"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[("project.md", b"divergent")],
        )


@pytest.mark.parametrize(
    "entry",
    [
        ("unknown.md", b"content"),
        ("../project.md", b"content"),
        ("project.md", bytearray(b"content")),
        ["project.md", b"content"],
        ("project.md",),
    ],
)
def test_downloaded_artifacts_require_exact_safe_entries(entry: object) -> None:
    manifest_json = _canonical_manifest()

    with pytest.raises(GenerationProjectionVerificationError):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json,
            artifacts=[entry],  # type: ignore[list-item]
        )


def test_manifest_requires_exact_bytes() -> None:
    manifest_json = _canonical_manifest()

    with pytest.raises(GenerationProjectionVerificationError, match="exact bytes"):
        verify_generation_projection(
            _target(manifest_json),
            manifest_json=manifest_json.decode(),  # type: ignore[arg-type]
            artifacts=[("project.md", b"# Project\n")],
        )


def test_verified_projection_and_nested_values_are_frozen() -> None:
    content = b"# Project\n"
    manifest_json = _canonical_manifest()
    projection = verify_generation_projection(
        _target(manifest_json),
        manifest_json=manifest_json,
        artifacts=[("project.md", content)],
    )

    with pytest.raises(FrozenInstanceError):
        projection.manifest_json = b"{}"
    with pytest.raises(FrozenInstanceError):
        projection.artifacts[0].content = b"changed"
    with pytest.raises(TypeError):
        projection.manifest.artifacts["project.md"] = "c" * 64
    with pytest.raises(pyd.ValidationError):
        projection.manifest.generation_id = OTHER_GENERATION_ID


def test_direct_projection_construction_revalidates_all_inputs() -> None:
    manifest_json = _canonical_manifest()
    target = _target(manifest_json)
    artifact = VerifiedGenerationArtifact("project.md", b"divergent")

    with pytest.raises(GenerationProjectionVerificationError, match="digest diverges"):
        VerifiedGenerationProjection(
            target=target,
            manifest_json=manifest_json,
            artifacts=(artifact,),
        )

    with pytest.raises(GenerationProjectionVerificationError, match="malformed"):
        VerifiedGenerationProjection(
            target=target,
            manifest_json=manifest_json,
            artifacts=[artifact],  # type: ignore[arg-type]
        )


def test_manifest_model_canonicalizes_without_mutable_artifact_map() -> None:
    manifest_json = _canonical_manifest()
    projection = verify_generation_projection(
        _target(manifest_json),
        manifest_json=manifest_json,
        artifacts=[("project.md", b"# Project\n")],
    )

    assert isinstance(projection.manifest, GenerationProjectionManifest)
    assert projection.manifest.canonical_bytes() == manifest_json
