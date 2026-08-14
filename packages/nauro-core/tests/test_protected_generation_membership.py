from __future__ import annotations

import pytest

from nauro_core.protected_generation_membership import (
    NoncanonicalGenerationPath,
    UnknownGenerationPath,
    is_hosted_snapshot_content_member,
    is_protected_generation_member,
    validate_protected_generation_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "project.md",
        "state.md",
        "state_current.md",
        "state_history.md",
        "stack.md",
        "open-questions.md",
        "questions-provenance.json",
        "decisions/001-use-sqlite.md",
        "context/handoff-one.md",
    ],
)
def test_protected_generation_members_are_closed(path: str) -> None:
    assert is_protected_generation_member(path)
    assert validate_protected_generation_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "project.md",
        "state.md",
        "state_current.md",
        "state_history.md",
        "stack.md",
        "open-questions.md",
        "decisions/001-use-sqlite.md",
    ],
)
def test_snapshot_members_are_independent(path: str) -> None:
    assert is_hosted_snapshot_content_member(path)


@pytest.mark.parametrize("path", ["questions-provenance.json", "context/handoff-one.md"])
def test_snapshot_excludes_private_projection_content(path: str) -> None:
    assert is_protected_generation_member(path)
    assert not is_hosted_snapshot_content_member(path)


@pytest.mark.parametrize(
    "path",
    [
        ".decision-hashes.json",
        "snapshots/v001.json",
        "journal/events.jsonl",
        "reports/report.md",
        "decisions/nested/001-bad.md",
        "unknown.md",
    ],
)
def test_unknown_paths_fail_closed(path: str) -> None:
    assert not is_protected_generation_member(path)
    with pytest.raises(UnknownGenerationPath):
        validate_protected_generation_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/project.md",
        "context//brief.md",
        "context/../brief.md",
        "context/./brief.md",
        "context\\brief.md",
    ],
)
def test_noncanonical_paths_reject_before_classification(path: str) -> None:
    with pytest.raises(NoncanonicalGenerationPath):
        validate_protected_generation_path(path)
