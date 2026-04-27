"""Tests for `nauro link --cloud`.

Three paths to cover:

1. Happy path: local-mode repo gets re-keyed to a server-minted ULID.
   The store directory is renamed and the registry entry is moved.
2. Already-cloud repo: nothing to link → clear error, no-op.
3. No-config repo: not a nauro repo → clear error.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.config import save_config
from nauro.store.repo_config import load_repo_config, save_repo_config
from nauro.sync import cloud_projects

runner = CliRunner()

CLOUD_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _seed_token(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    save_config({"auth": {"access_token": "test-token", "sub": "auth0|test"}})


def _create_response(name: str = "linkproj", project_id: str = CLOUD_PID):
    def handler(method, url, **kwargs):
        return httpx.Response(
            201,
            json={
                "project_id": project_id,
                "name": name,
                "role": "owner",
                "created_at": "2026-04-27T00:00:00Z",
            },
            request=httpx.Request(method, url),
        )

    return handler


def test_link_cloud_promotes_local_project(tmp_path, monkeypatch):
    """The store dir is moved to the cloud id and the registry entry re-keyed."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    # Stand up a local project from scratch
    init_result = runner.invoke(app, ["init", "linkproj"])
    assert init_result.exit_code == 0, init_result.output

    matches = registry.find_projects_by_name_v2("linkproj")
    assert len(matches) == 1
    local_id, _entry = matches[0]
    local_store = tmp_path / "nauro_home" / "projects" / local_id
    assert local_store.is_dir()

    # Mark the store with a sentinel so we can prove the rename moved its contents
    sentinel = local_store / "decisions" / "999-sentinel.md"
    sentinel.write_text("# 999 — sentinel\n")

    with patch.object(cloud_projects.httpx, "request", side_effect=_create_response()):
        result = runner.invoke(app, ["link", "--cloud"])
    assert result.exit_code == 0, result.output

    new_store = tmp_path / "nauro_home" / "projects" / CLOUD_PID
    assert new_store.is_dir()
    assert (new_store / "decisions" / "999-sentinel.md").exists()
    assert not local_store.exists()

    # Registry entry re-keyed under the cloud id, mode flipped, repo_paths preserved
    assert registry.get_project_v2(local_id) is None
    new_entry = registry.get_project_v2(CLOUD_PID)
    assert new_entry is not None
    assert new_entry["mode"] == "cloud"
    assert str(tmp_path.resolve()) in new_entry["repo_paths"]

    cfg = load_repo_config(tmp_path)
    assert cfg["mode"] == "cloud"
    assert cfg["id"] == CLOUD_PID


def test_link_cloud_on_already_cloud_repo_errors(tmp_path, monkeypatch):
    """A cloud-mode repo cannot be linked again."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    save_repo_config(
        tmp_path,
        {
            "mode": "cloud",
            "id": CLOUD_PID,
            "name": "already",
            "server_url": "https://example.test",
        },
    )

    with patch.object(cloud_projects.httpx, "request", side_effect=AssertionError("no call")):
        result = runner.invoke(app, ["link", "--cloud"])

    assert result.exit_code == 1
    assert "already cloud-mode" in result.output


def test_link_cloud_with_no_repo_config_errors(tmp_path, monkeypatch):
    """No `.nauro/config.json` above cwd → clear error, no network call."""
    _seed_token(monkeypatch, tmp_path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    with patch.object(cloud_projects.httpx, "request", side_effect=AssertionError("no call")):
        result = runner.invoke(app, ["link", "--cloud"])

    assert result.exit_code == 1
    assert "Not a nauro repo" in result.output
