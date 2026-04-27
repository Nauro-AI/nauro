"""Tests for `nauro attach <project_id>`.

Membership is verified against ``GET /projects`` before any local state
is written; the non-member error path explicitly asserts no registry
side effects to keep the failure mode safe.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.config import save_config
from nauro.store.repo_config import load_repo_config
from nauro.sync import cloud_projects

runner = CliRunner()

EXAMPLE_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _seed_token(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))
    save_config({"auth": {"access_token": "test-token", "sub": "auth0|test"}})


def _list_response(projects):
    def handler(method, url, **kwargs):
        return httpx.Response(200, json=projects, request=httpx.Request(method, url))

    return handler


def test_attach_happy_path(tmp_path, monkeypatch):
    """A member of the cloud project gets a v2 entry + cloud-mode repo config."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    handler = _list_response(
        [
            {
                "project_id": EXAMPLE_PID,
                "name": "team-proj",
                "role": "viewer",
                "created_at": "2026-04-27T00:00:00Z",
            }
        ]
    )

    with patch.object(cloud_projects.httpx, "request", side_effect=handler):
        result = runner.invoke(app, ["attach", EXAMPLE_PID])

    assert result.exit_code == 0, result.output
    entry = registry.get_project_v2(EXAMPLE_PID)
    assert entry is not None
    assert entry["mode"] == "cloud"
    assert entry["name"] == "team-proj"
    assert str(tmp_path.resolve()) in entry["repo_paths"]

    cfg = load_repo_config(tmp_path)
    assert cfg["mode"] == "cloud"
    assert cfg["id"] == EXAMPLE_PID
    assert cfg["name"] == "team-proj"

    store_path = tmp_path / "nauro_home" / "projects" / EXAMPLE_PID
    assert store_path.is_dir()


def test_attach_non_member_writes_nothing(tmp_path, monkeypatch):
    """When the user is not a member, the registry and config are untouched."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    handler = _list_response([])

    with patch.object(cloud_projects.httpx, "request", side_effect=handler):
        result = runner.invoke(app, ["attach", EXAMPLE_PID])

    assert result.exit_code == 1
    assert "not found among your cloud projects" in result.output
    assert registry.get_project_v2(EXAMPLE_PID) is None
    assert not (tmp_path / ".nauro" / "config.json").exists()
    assert not (tmp_path / "nauro_home" / "projects" / EXAMPLE_PID).exists()
