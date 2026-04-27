"""Tests for the v2 project-resolution flow used by every CLI command.

The CLI's ``resolve_target_project`` and the local MCP servers'
``_resolve_store`` share the same priority order: explicit ``--project``
flag → cwd ``.nauro/config.json`` walk-up → v1 legacy fallback. These
tests pin that behavior end-to-end so a regression cannot silently flip
the precedence.

Also covers the integration scenario where two projects coexist (one
cloud-style preconfigured, one freshly created via `nauro init`) and
each cwd resolves to its own store.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.cli.utils import resolve_target_project
from nauro.constants import REGISTRY_FILENAME, REGISTRY_SCHEMA_VERSION_V2
from nauro.mcp.stdio_server import _resolve_store
from nauro.store import registry
from nauro.store.repo_config import load_repo_config, save_repo_config

runner = CliRunner()


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))


def _post_migration_state(tmp_path, monkeypatch):
    """Simulate the post-manual-migration state: v2 registry + cloud repo config."""
    _patch_home(monkeypatch, tmp_path)
    nauro_home = tmp_path / "nauro_home"
    nauro_home.mkdir()
    cloud_pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo_root = tmp_path / "cloud_repo"
    repo_root.mkdir()
    (nauro_home / "projects" / cloud_pid).mkdir(parents=True)
    (nauro_home / REGISTRY_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": REGISTRY_SCHEMA_VERSION_V2,
                "projects": {
                    cloud_pid: {
                        "name": "nauro",
                        "mode": "cloud",
                        "server_url": "https://mcp.nauro.ai",
                        "repo_paths": [str(repo_root.resolve())],
                    }
                },
            }
        )
        + "\n"
    )
    save_repo_config(
        repo_root,
        {
            "mode": "cloud",
            "id": cloud_pid,
            "name": "nauro",
            "server_url": "https://mcp.nauro.ai",
        },
    )
    return cloud_pid, repo_root


# ── repo config wins over cwd-only registry lookup ───────────────────────────


def test_repo_config_resolves_to_id_keyed_store(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    name, store = resolve_target_project(None)
    assert name == "nauro"
    assert store == tmp_path / "nauro_home" / "projects" / cloud_pid


def test_resolution_walks_up_from_nested_dir(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    nested = repo_root / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    _name, store = resolve_target_project(None)
    assert store == tmp_path / "nauro_home" / "projects" / cloud_pid


def test_explicit_project_flag_overrides_repo_config(tmp_path, monkeypatch):
    """`--project <other-name>` takes precedence over the cwd config.

    Existing test_cli.py expectation; pinned here against the v2 path.
    """
    _patch_home(monkeypatch, tmp_path)
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    other_pid, _store = registry.register_project_v2(
        "beta",
        [tmp_path / "beta_repo"],
        mode="local",
    )
    (tmp_path / "beta_repo").mkdir()
    monkeypatch.chdir(repo_root)
    name, store = resolve_target_project("beta")
    assert name == "beta"
    assert store == tmp_path / "nauro_home" / "projects" / other_pid


# ── stdio _resolve_store mirrors the same precedence ─────────────────────────


def test_stdio_resolve_uses_repo_config_when_no_project_passed(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    store = _resolve_store(None, None)
    assert store == tmp_path / "nauro_home" / "projects" / cloud_pid


def test_stdio_resolve_mismatched_project_id_errors(tmp_path, monkeypatch):
    """When supplied project_id != repo config id, raise rather than silently swap."""
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    with pytest.raises(ValueError, match="does not match"):
        _resolve_store("01KZZZZZZZZZZZZZZZZZZZZZZZ", str(repo_root))


def test_stdio_resolve_no_repo_no_project_errors(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)
    with pytest.raises(ValueError, match="Could not resolve project"):
        _resolve_store(None, None)


def test_stdio_resolve_explicit_id_matching_config(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    store = _resolve_store(cloud_pid, str(repo_root))
    assert store == tmp_path / "nauro_home" / "projects" / cloud_pid


# ── integration: two projects coexist post-migration ─────────────────────────


def test_two_projects_each_resolve_to_own_store(tmp_path, monkeypatch):
    """Pre-migrated cloud project + a freshly-created local project don't collide."""
    cloud_pid, cloud_repo = _post_migration_state(tmp_path, monkeypatch)

    other = tmp_path / "other_repo"
    other.mkdir()
    monkeypatch.chdir(other)
    result = runner.invoke(app, ["init", "side"])
    assert result.exit_code == 0, result.output

    matches = registry.find_projects_by_name_v2("side")
    assert len(matches) == 1
    side_pid, _entry = matches[0]
    assert side_pid != cloud_pid

    # cwd in cloud repo → cloud store
    monkeypatch.chdir(cloud_repo)
    _, store = resolve_target_project(None)
    assert store == tmp_path / "nauro_home" / "projects" / cloud_pid

    # cwd in other repo → side store
    monkeypatch.chdir(other)
    _, store = resolve_target_project(None)
    assert store == tmp_path / "nauro_home" / "projects" / side_pid

    # Cloud-mode config not corrupted by the new local-mode init in a different dir
    cfg = load_repo_config(cloud_repo)
    assert cfg["mode"] == "cloud"
    assert cfg["id"] == cloud_pid
