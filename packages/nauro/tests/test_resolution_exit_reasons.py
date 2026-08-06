"""Reason/guidance metadata on ``resolve_target_project`` failures.

Every raise site in ``resolve_target_project`` attaches a pinned reason
value and the exact stderr message as ``guidance``, so structured
consumers (``nauro status --json``) can re-surface the failure without
re-deriving the prose. Stderr output, exit codes, and the
``DisconnectedProjectExit`` isinstance identity are unchanged — the
existing resolution tests pin that separately.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nauro.cli.utils import (
    RESOLUTION_AMBIGUOUS_PROJECT,
    RESOLUTION_DISCONNECTED_PROJECT,
    RESOLUTION_NO_PROJECT,
    RESOLUTION_UNKNOWN_PROJECT,
    DisconnectedProjectExit,
    ProjectResolutionExit,
    resolve_target_project,
)
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config


def _resolution_failure(capsys, project_flag: str | None) -> tuple[ProjectResolutionExit, str]:
    with pytest.raises(ProjectResolutionExit) as excinfo:
        resolve_target_project(project_flag)
    return excinfo.value, capsys.readouterr().err


def test_ambiguous_project(tmp_path: Path, capsys) -> None:
    register_project_v2("dup", [tmp_path / "a"])
    register_project_v2("dup", [tmp_path / "b"])

    exc, err = _resolution_failure(capsys, "dup")

    assert exc.reason == RESOLUTION_AMBIGUOUS_PROJECT
    assert exc.exit_code == 1
    assert exc.guidance.startswith("Multiple projects named 'dup'. Disambiguate by id:")
    assert err == exc.guidance + "\n"


def test_unknown_project(tmp_path: Path, capsys) -> None:
    register_project_v2("known", [tmp_path / "a"])

    exc, err = _resolution_failure(capsys, "missing")

    assert exc.reason == RESOLUTION_UNKNOWN_PROJECT
    assert exc.exit_code == 1
    assert exc.guidance == "Unknown project 'missing'.\nAvailable projects: known"
    assert err == exc.guidance + "\n"


def test_disconnected_project_via_project_flag(tmp_path: Path, capsys) -> None:
    _pid, store_path = register_project_v2("gone", [tmp_path / "a"])
    shutil.rmtree(store_path)

    exc, err = _resolution_failure(capsys, "gone")

    assert isinstance(exc, DisconnectedProjectExit)
    assert exc.reason == RESOLUTION_DISCONNECTED_PROJECT
    assert exc.exit_code == 1
    assert "nauro reconnect" in exc.guidance
    assert err == exc.guidance + "\n"


def test_disconnected_project_via_cwd(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    save_repo_config(repo, {"mode": "local", "id": "01KQ6AZGNA0B3QBF67NBXP3S45", "name": "Pareto"})
    monkeypatch.chdir(repo)

    exc, err = _resolution_failure(capsys, None)

    assert isinstance(exc, DisconnectedProjectExit)
    assert exc.reason == RESOLUTION_DISCONNECTED_PROJECT
    assert exc.exit_code == 1
    assert err == exc.guidance + "\n"


def test_no_project(tmp_path: Path, monkeypatch, capsys) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    exc, err = _resolution_failure(capsys, None)

    assert exc.reason == RESOLUTION_NO_PROJECT
    assert exc.exit_code == 1
    assert exc.guidance == "No project found for current directory.\nRun 'nauro init' first."
    assert err == exc.guidance + "\n"
