"""Tests for `nauro attach <project_id>`.

Membership is verified against ``GET /projects`` before any local state
is written; the non-member error path explicitly asserts no registry
side effects to keep the failure mode safe.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.repo_config import load_repo_config
from nauro.sync import cloud_projects
from tests.conftest import seed_auth_config

runner = CliRunner()

EXAMPLE_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _seed_token(monkeypatch, tmp_path):
    seed_auth_config()


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

    store_path = tmp_path / "projects" / EXAMPLE_PID
    assert store_path.is_dir()


def test_attach_from_home_is_refused_before_any_network_call(tmp_path, monkeypatch):
    """A repo path whose .nauro/config.json is the global config is refused.

    The guard fires before the membership lookup, so no token and no mocked
    transport are needed: an unpatched httpx call here would be a test
    failure in itself.
    """
    home = tmp_path / "home"
    nauro_home = home / ".nauro"
    nauro_home.mkdir(parents=True)
    monkeypatch.setenv("NAURO_HOME", str(nauro_home))
    sentinel = '{"auth": {"access_token": "keep-me"}}\n'
    (nauro_home / "config.json").write_text(sentinel)
    monkeypatch.chdir(home)

    result = runner.invoke(app, ["attach", EXAMPLE_PID])

    assert result.exit_code == 1
    assert "global config" in result.output
    # Telemetry bookkeeping may merge into the file on any CLI run; the auth
    # block must survive and no repo-config keys may appear.
    data = json.loads((nauro_home / "config.json").read_text())
    assert data["auth"] == {"access_token": "keep-me"}
    assert "mode" not in data
    assert registry.get_project_v2(EXAMPLE_PID) is None


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
    assert not (tmp_path / "projects" / EXAMPLE_PID).exists()
