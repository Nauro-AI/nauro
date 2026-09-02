from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import nauro.store.generation_migration_plan as migration_plan
from nauro.store.generation_authority import GenerationProjectionIdentity
from nauro.store.generation_migration_assessment import (
    LegacyMigrationAssessment,
    assess_legacy_migration,
)
from nauro.store.generation_migration_plan import (
    LegacyMigrationPlan,
    LegacyMigrationPlanEntry,
    LegacyMigrationPlanError,
    prepare_legacy_migration_plan,
)
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K33333333333333333333333"
MIGRATION_ID = "01K44444444444444444444444"
PROJECTION_SCOPE_ID = "a" * 64


def _binding(tmp_path: Path) -> ResolvedProjectBinding:
    store = tmp_path / "projects" / PROJECT_ID
    store.mkdir(parents=True)
    return ResolvedProjectBinding(
        store_path=store,
        project_id=PROJECT_ID,
        display_name="Nauro",
        mode="cloud",
        server_url="https://mcp.nauro.ai",
    )


def _projection(
    binding: ResolvedProjectBinding,
    artifacts: dict[str, bytes],
) -> VerifiedGenerationProjection:
    manifest_json = json.dumps(
        {
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": GENERATION_ID,
            "projection_class": "contributor_plus",
            "projection_scope_id": PROJECTION_SCOPE_ID,
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
        generation_id=GENERATION_ID,
        manifest_digest=hashlib.sha256(manifest_json).hexdigest(),
        committed_at="2026-09-02T01:00:00.000000Z",
        installed_for_user_id=USER_ID,
        projection_class="contributor_plus",
        projection_scope_id=PROJECTION_SCOPE_ID,
    )
    return verify_generation_projection(
        GenerationProjectionTarget(binding=binding, identity=identity),
        manifest_json=manifest_json,
        artifacts=list(artifacts.items()),
    )


def _write(store: Path, relative: str, content: bytes) -> None:
    path = store / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _assessment(tmp_path: Path) -> tuple[ResolvedProjectBinding, LegacyMigrationAssessment]:
    binding = _binding(tmp_path)
    local = {
        "project.md": b"# Project\n",
        "state_current.md": b"# Local state\n",
        "state_history.md": b"# Local history\n",
        "stack.md": b"# Local stack\n",
        "open-questions.md": b"# Local questions\n",
        "questions-provenance.json": b"{}\n",
        "decisions/001-one.md": b"# D1: One\n",
        "decisions/002-two.md": b"# D2: Two\n",
        "context/auth-cutover-2.md": b"# Brief\n",
    }
    for path, content in local.items():
        _write(binding.store_path, path, content)
    _write(binding.store_path, "snapshots/incident.json", b'{"incident":true}\n')
    projection = _projection(
        binding,
        {
            "project.md": b"# Server project\n",
            "state_current.md": b"# Server state\n",
            "state.md": b"# Server legacy state\n",
            "decisions/001-one.md": local["decisions/001-one.md"],
        },
    )
    return binding, assess_legacy_migration(projection)


def _entry(plan: LegacyMigrationPlan, path: str) -> LegacyMigrationPlanEntry:
    return next(entry for entry in plan.entries if entry.source_path == path)


def test_plan_binds_every_assessed_byte_to_backup_or_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, assessment = _assessment(tmp_path)
    monkeypatch.setattr(migration_plan, "generate_ulid", lambda: MIGRATION_ID)

    plan = prepare_legacy_migration_plan(assessment)

    assert set(plan.entries) == set(plan.backup_entries) | set(plan.quarantine_entries)
    assert set(plan.backup_entries).isdisjoint(plan.quarantine_entries)
    assert {entry.source_path for entry in plan.entries} == {
        *(stamp.path for stamp in assessment.protected_files),
        *(stamp.path for stamp in assessment.snapshot_files),
    }
    assert _entry(plan, "decisions/001-one.md").destination_path == ("legacy/decisions/001-one.md")
    assert _entry(plan, "decisions/001-one.md").recovery_kind == "restore_only"
    assert _entry(plan, "project.md").destination_path == "quarantine/project.md"
    assert _entry(plan, "project.md").recovery_routes == ("project_frame",)
    assert _entry(plan, "state_current.md").destination_path == ("quarantine/state_current.md")
    assert _entry(plan, "state_current.md").recovery_routes == ("update_state",)
    assert _entry(plan, "stack.md").recovery_routes == ("update_stack",)
    assert _entry(plan, "open-questions.md").recovery_routes == (
        "flag_question",
        "propose_decision",
    )
    assert _entry(plan, "decisions/002-two.md").recovery_routes == ("propose_decision",)
    assert _entry(plan, "context/auth-cutover-2.md").recovery_routes == ("share_context",)
    assert _entry(plan, "questions-provenance.json").recovery_kind == ("preserved_unsupported")
    assert _entry(plan, "questions-provenance.json").offer_export is True
    assert _entry(plan, "state_history.md").recovery_kind == "preserved_unsupported"
    assert _entry(plan, "state_history.md").offer_export is True
    assert _entry(plan, "snapshots/incident.json").source_class == "snapshot"
    assert _entry(plan, "snapshots/incident.json").recovery_kind == "archive_only"
    assert _entry(plan, "snapshots/incident.json").offer_export is True
    assert _entry(plan, "snapshots/incident.json").destination_path == (
        "legacy/snapshots/incident.json"
    )
    assert plan.backup_root.parent == binding.store_path.parent
    assert plan.backup_root != binding.store_path
    assert not plan.backup_root.exists()


def test_plan_manifest_is_canonical_and_binds_projection_and_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, assessment = _assessment(tmp_path)
    monkeypatch.setattr(migration_plan, "generate_ulid", lambda: MIGRATION_ID)

    plan = prepare_legacy_migration_plan(assessment)
    manifest = json.loads(plan.manifest_json)

    assert (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        == plan.manifest_json
    )
    assert manifest["schema_version"] == 1
    assert manifest["project_id"] == PROJECT_ID
    assert manifest["migration_id"] == MIGRATION_ID
    assert manifest["assessment_capture_digest"] == assessment.capture_digest
    assert manifest["projection"] == assessment.projection.target.identity.model_dump()
    assert manifest["server_only_paths"] == ["state.md"]
    assert hashlib.sha256(plan.manifest_json).hexdigest() == plan.plan_digest
    assert "/" not in plan.backup_directory_name
    assert "\\" not in plan.backup_directory_name
    assert str(tmp_path).encode() not in plan.manifest_json


def test_plan_rejects_unsettled_assessment(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write(binding.store_path, "project.md", b"# Project\n")
    _write(binding.store_path, ".project.md.0123456789abcdef.tmp", b"pending")
    assessment = assess_legacy_migration(_projection(binding, {"project.md": b"# Project\n"}))

    with pytest.raises(LegacyMigrationPlanError, match="settled pending") as raised:
        prepare_legacy_migration_plan(assessment)

    assert raised.value.code == "legacy_migration_plan_failed"


def test_plan_requires_exact_assessment() -> None:
    with pytest.raises(LegacyMigrationPlanError) as raised:
        prepare_legacy_migration_plan(object())  # type: ignore[arg-type]

    assert raised.value.code == "legacy_migration_plan_failed"


def test_plan_rejects_invalid_derived_migration_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, assessment = _assessment(tmp_path)
    monkeypatch.setattr(migration_plan, "generate_ulid", lambda: "migration")

    with pytest.raises(LegacyMigrationPlanError, match="identity") as raised:
        prepare_legacy_migration_plan(assessment)

    assert raised.value.code == "legacy_migration_plan_failed"


def test_plan_cannot_be_constructed_without_preparation(tmp_path: Path) -> None:
    _, assessment = _assessment(tmp_path)

    with pytest.raises(LegacyMigrationPlanError) as raised:
        LegacyMigrationPlan(
            assessment=assessment,
            migration_id=MIGRATION_ID,
            created_at="2026-09-02T01:00:00.000000Z",
            backup_directory_name="legacy-backup",
            entries=(),
            manifest_json=b"{}",
            plan_digest=hashlib.sha256(b"{}").hexdigest(),
            _construction_token=object(),
        )

    assert raised.value.code == "legacy_migration_plan_failed"
