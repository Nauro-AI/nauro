from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from nauro.store.generation_migration_assessment import (
    LegacyMigrationAssessment,
    LegacyMigrationAssessmentError,
    assess_legacy_migration,
)
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K33333333333333333333333"
PROJECTION_SCOPE_ID = "a" * 64


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
    artifacts: dict[str, bytes],
) -> VerifiedGenerationProjection:
    manifest_json = json.dumps(
        {
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": GENERATION_ID,
            "committed_at": "2026-09-02T01:00:00.000000Z",
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
        artifacts=tuple(artifacts.items()),
    )


def _write(store: Path, relative: str, content: bytes) -> None:
    path = store / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_assessment_classifies_protected_paths_and_stamps_snapshots(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = binding.store_path
    local = {
        "project.md": b"# Project\n",
        "state_current.md": b"# Local state\n",
        "stack.md": b"# Local stack\n",
        "decisions/001-one.md": b"# D1: One\n",
        "context/auth-cutover-2.md": b"# Brief\n",
    }
    for path, content in local.items():
        _write(store, path, content)
    _write(store, "project.md.lock", b"")
    _write(store, "decisions/.lock", b"")
    _write(store, "decisions/001-one.md.lock", b"")
    _write(store, "context/.lock", b"")
    _write(store, "context/auth-cutover-2.md.lock", b"")
    snapshot = b'{"captured":true}\n'
    _write(store, "snapshots/2026-09-02.json", snapshot)
    _write(store, "snapshots/.lock", b"")
    projection = _projection(
        binding,
        {
            "project.md": local["project.md"],
            "state_current.md": b"# Server state\n",
            "open-questions.md": b"# Questions\n",
            "decisions/001-one.md": local["decisions/001-one.md"],
        },
    )

    assessment = assess_legacy_migration(projection)

    assert assessment.identical_paths == ("decisions/001-one.md", "project.md")
    assert assessment.divergent_paths == ("state_current.md",)
    assert assessment.local_only_paths == ("context/auth-cutover-2.md", "stack.md")
    assert assessment.server_only_paths == ("open-questions.md",)
    assert assessment.nonidentical_local_paths == (
        "context/auth-cutover-2.md",
        "stack.md",
        "state_current.md",
    )
    assert assessment.pending_paths == ()
    assert assessment.unsupported_paths == (
        "context/.lock",
        "context/auth-cutover-2.md.lock",
        "decisions/.lock",
        "decisions/001-one.md.lock",
        "project.md.lock",
        "snapshots/.lock",
    )
    assert assessment.ready_for_backup is True
    assert tuple(stamp.path for stamp in assessment.protected_files) == tuple(sorted(local))
    assert assessment.snapshot_files[0].path == "snapshots/2026-09-02.json"
    assert assessment.snapshot_files[0].size == len(snapshot)
    assert assessment.snapshot_files[0].sha256 == hashlib.sha256(snapshot).hexdigest()
    assert len(assessment.capture_digest) == 64
    assert not (store / ".replica").exists()
    assert {path: (store / path).read_bytes() for path in local} == local


def test_assessment_surfaces_pending_and_unsupported_entries(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = binding.store_path
    _write(store, "project.md", b"# Project\n")
    _write(store, ".project.md.0123456789abcdef.tmp", b"pending")
    (store / ".pull-spool-interrupted").mkdir()
    _write(store, "decisions/.002-two.md.0123456789abcdef.tmp", b"pending")
    _write(store, "decisions/bad.txt", b"unsupported")
    _write(store, "context/bad.txt", b"unsupported")
    _write(store, "snapshots/.snapshot.json.0123456789abcdef.tmp", b"pending")
    (store / "snapshots" / "nested").mkdir()
    projection = _projection(binding, {"project.md": b"# Project\n"})

    assessment = assess_legacy_migration(projection)

    assert assessment.pending_paths == (
        ".project.md.0123456789abcdef.tmp",
        ".pull-spool-interrupted",
        "decisions/.002-two.md.0123456789abcdef.tmp",
        "snapshots/.snapshot.json.0123456789abcdef.tmp",
    )
    assert assessment.unsupported_paths == (
        ".project.md.0123456789abcdef.tmp",
        "context/bad.txt",
        "decisions/.002-two.md.0123456789abcdef.tmp",
        "decisions/bad.txt",
    )
    assert assessment.ready_for_backup is False


def test_assessment_refuses_linked_protected_file_without_reading_outside(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"retain")
    linked = binding.store_path / "project.md"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("platform does not permit test symlinks")
    projection = _projection(binding, {"project.md": b"retain"})

    with pytest.raises(LegacyMigrationAssessmentError) as raised:
        assess_legacy_migration(projection)

    assert raised.value.code == "legacy_migration_assessment_failed"
    assert outside.read_bytes() == b"retain"


def test_assessment_refuses_generation_marker(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write(binding.store_path, "project.md", b"# Project\n")
    _write(binding.store_path, ".replica/authority.json", b"{}")
    projection = _projection(binding, {"project.md": b"# Project\n"})

    with pytest.raises(
        LegacyMigrationAssessmentError,
        match="requires absent generation authority",
    ) as raised:
        assess_legacy_migration(projection)

    assert raised.value.code == "legacy_migration_assessment_failed"


def test_assessment_capture_is_repeatable_and_changes_with_legacy_bytes(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    project = binding.store_path / "project.md"
    project.write_bytes(b"# Project\n")
    projection = _projection(binding, {"project.md": b"# Project\n"})

    first = assess_legacy_migration(projection)
    second = assess_legacy_migration(projection)
    project.write_bytes(b"# Changed\n")
    changed = assess_legacy_migration(projection)

    assert second.capture_digest == first.capture_digest
    assert changed.capture_digest != first.capture_digest
    assert changed.divergent_paths == ("project.md",)


def test_assessment_detects_atomic_replacement_during_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    project = binding.store_path / "project.md"
    project.write_bytes(b"# Project\n")
    projection = _projection(binding, {"project.md": b"# Project\n"})
    original_fdopen = os.fdopen

    def replace_after_open(descriptor: int, *args: object, **kwargs: object):
        handle = original_fdopen(descriptor, *args, **kwargs)
        replacement = project.with_suffix(".replacement")
        replacement.write_bytes(b"# Changed\n")
        replacement.replace(project)
        return handle

    monkeypatch.setattr(os, "fdopen", replace_after_open)

    with pytest.raises(LegacyMigrationAssessmentError, match="changed during read"):
        assess_legacy_migration(projection)


def test_assessment_cannot_be_constructed_without_scan(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    projection = _projection(binding, {"project.md": b"# Project\n"})

    with pytest.raises(LegacyMigrationAssessmentError) as raised:
        LegacyMigrationAssessment(
            projection=projection,
            protected_files=(),
            snapshot_files=(),
            evidence_files=(),
            directory_paths=(),
            identical_paths=(),
            divergent_paths=(),
            local_only_paths=(),
            server_only_paths=(),
            pending_paths=(),
            unsupported_paths=(),
            capture_digest="0" * 64,
            _construction_token=object(),
        )

    assert raised.value.code == "legacy_migration_assessment_failed"


def test_assessment_requires_exact_verified_projection() -> None:
    with pytest.raises(LegacyMigrationAssessmentError) as raised:
        assess_legacy_migration(object())  # type: ignore[arg-type]

    assert raised.value.code == "legacy_migration_assessment_failed"


@pytest.mark.parametrize(
    "relative",
    [".decision-hashes.json", ".sync-state.json", "journal/events.jsonl", "unknown/nested/raw.bin"],
)
def test_assessment_preserves_unknown_bytes_without_inventing_pending_work(
    tmp_path: Path, relative: str
) -> None:
    binding = _binding(tmp_path)
    content = b"\x00preserve\xff"
    _write(binding.store_path, relative, content)
    assessment = assess_legacy_migration(_projection(binding, {}))
    assert assessment.pending_paths == ()
    assert assessment.ready_for_backup is True
    assert [(stamp.path, stamp.size, stamp.sha256) for stamp in assessment.evidence_files] == [
        (relative, len(content), hashlib.sha256(content).hexdigest())
    ]
    assert (binding.store_path / relative).read_bytes() == content


@pytest.mark.parametrize("kind", ["empty", "dangling", "partial"])
def test_assessment_refuses_any_replica_evidence(tmp_path: Path, kind: str) -> None:
    binding = _binding(tmp_path)
    control = binding.store_path / ".replica"
    if kind == "dangling":
        control.symlink_to(tmp_path / "missing")
    else:
        control.mkdir()
        if kind == "partial":
            (control / "intent.json").write_bytes(b"{}")
    with pytest.raises(LegacyMigrationAssessmentError, match="absent generation authority"):
        assess_legacy_migration(_projection(binding, {}))


@pytest.mark.parametrize("relative", ["unknown", "unknown/nested", ".decision-hashes.json"])
def test_assessment_refuses_links_in_nonprotected_evidence(tmp_path: Path, relative: str) -> None:
    binding = _binding(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"retain")
    link = binding.store_path / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    with pytest.raises(LegacyMigrationAssessmentError):
        assess_legacy_migration(_projection(binding, {}))
    assert outside.read_bytes() == b"retain"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO fixture requires POSIX")
def test_assessment_refuses_special_file(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    os.mkfifo(binding.store_path / "unknown")
    with pytest.raises(LegacyMigrationAssessmentError, match="not regular"):
        assess_legacy_migration(_projection(binding, {}))


@pytest.mark.parametrize("relative", [".replica", "unknown.bin", "nested"])
def test_inspection_error_refuses_without_losing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    binding = _binding(tmp_path)
    path = binding.store_path / relative
    if relative == "nested":
        path.mkdir()
        (path / "evidence").write_bytes(b"retain")
    elif relative != ".replica":
        path.write_bytes(b"retain")
    original = Path.lstat

    def unavailable(candidate: Path):
        if candidate == path:
            raise PermissionError("inspection denied")
        return original(candidate)

    monkeypatch.setattr(Path, "lstat", unavailable)
    with pytest.raises(LegacyMigrationAssessmentError):
        assess_legacy_migration(_projection(binding, {}))
    if relative == "nested":
        assert (path / "evidence").read_bytes() == b"retain"
    elif relative != ".replica":
        assert path.read_bytes() == b"retain"


def test_evidence_and_empty_directories_change_capture_digest(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    path = binding.store_path / ".decision-hashes.json"
    path.write_bytes(b"first")
    projection = _projection(binding, {})
    first = assess_legacy_migration(projection)
    path.write_bytes(b"second")
    second = assess_legacy_migration(projection)
    assert first.capture_digest != second.capture_digest
    (binding.store_path / "empty").mkdir()
    third = assess_legacy_migration(projection)
    assert second.capture_digest != third.capture_digest
    assert third.directory_paths == ("empty",)


def test_nonempty_control_lock_is_refused_without_truncation(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    content = b"old evidence"
    path = binding.store_path / ".replica-control.lock"
    path.write_bytes(content)
    with pytest.raises(LegacyMigrationAssessmentError, match="unsafe evidence"):
        assess_legacy_migration(_projection(binding, {}))
    assert path.read_bytes() == content
