"""Tests for `nauro init --cloud` and the local/cloud split.

Local mode: no network, mints a local ULID, writes v2 registry +
``.nauro/config.json`` in cwd.

Cloud mode: calls the remote MCP server's ``POST /projects`` (mocked),
uses the server-minted project_id for the local store, writes the
config in cloud mode.

The cloud-mode add-repo error path is here too — adding a repo to a
cloud-scoped project must point the user at ``nauro attach``.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.repo_config import load_repo_config
from nauro.sync import cloud_projects

runner = CliRunner()


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))


def _seed_token(monkeypatch, tmp_path):
    """Make cloud_projects believe the user is authenticated."""
    from nauro.store.config import save_config

    _patch_home(monkeypatch, tmp_path)
    save_config({"auth": {"access_token": "test-token", "sub": "auth0|test"}})


# ── local mode ────────────────────────────────────────────────────────────────


def test_init_local_no_network(tmp_path, monkeypatch):
    """`nauro init <name>` (no flag) must not contact the cloud."""
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    def explode(*_args, **_kwargs):
        raise AssertionError("local init should not call httpx")

    with patch.object(cloud_projects.httpx, "request", side_effect=explode):
        result = runner.invoke(app, ["init", "localproj"])

    assert result.exit_code == 0, result.output
    matches = registry.find_projects_by_name_v2("localproj")
    assert len(matches) == 1
    pid, entry = matches[0]
    assert entry["mode"] == "local"
    assert "server_url" not in entry

    cfg = load_repo_config(tmp_path)
    assert cfg["mode"] == "local"
    assert cfg["id"] == pid
    assert cfg["name"] == "localproj"
    assert (tmp_path / "nauro_home" / "projects" / pid / "project.md").exists()


# ── cloud mode ────────────────────────────────────────────────────────────────


def test_init_cloud_uses_server_minted_id(tmp_path, monkeypatch):
    """`nauro init --cloud` mirrors the server's project_id locally."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    server_id = "01KQ6AZGNA0B3QBF67NBXP3S45"

    def handler(method, url, **kwargs):
        assert method == "POST"
        assert url == "https://example.test/projects"
        assert kwargs["json"] == {"name": "cloudproj"}
        return httpx.Response(
            201,
            json={
                "project_id": server_id,
                "name": "cloudproj",
                "role": "owner",
                "created_at": "2026-04-27T00:00:00Z",
            },
            request=httpx.Request(method, url),
        )

    with patch.object(cloud_projects.httpx, "request", side_effect=handler):
        result = runner.invoke(app, ["init", "--cloud", "cloudproj"])

    assert result.exit_code == 0, result.output
    entry = registry.get_project_v2(server_id)
    assert entry is not None
    assert entry["mode"] == "cloud"
    assert entry["server_url"]
    assert entry["name"] == "cloudproj"

    cfg = load_repo_config(tmp_path)
    assert cfg["mode"] == "cloud"
    assert cfg["id"] == server_id
    assert (tmp_path / "nauro_home" / "projects" / server_id / "project.md").exists()


def test_init_cloud_renders_server_error(tmp_path, monkeypatch):
    """A cloud failure surfaces the server message and writes nothing locally."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        return httpx.Response(503, request=httpx.Request(method, url))

    with patch.object(cloud_projects.httpx, "request", side_effect=handler):
        result = runner.invoke(app, ["init", "--cloud", "cloudproj"])

    assert result.exit_code == 1
    assert "503" in result.output
    assert registry.find_projects_by_name_v2("cloudproj") == []


# ── add-repo against cloud-mode project ───────────────────────────────────────


def test_add_repo_to_cloud_project_errors_with_attach_hint(tmp_path, monkeypatch):
    """`nauro init <cloud-name> --add-repo …` must point users at attach."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")
    server_id = "01KQ6AZGNA0B3QBF67NBXP3S45"

    def create_handler(method, url, **kwargs):
        return httpx.Response(
            201,
            json={
                "project_id": server_id,
                "name": "cloudproj",
                "role": "owner",
                "created_at": "2026-04-27T00:00:00Z",
            },
            request=httpx.Request(method, url),
        )

    with patch.object(cloud_projects.httpx, "request", side_effect=create_handler):
        first = runner.invoke(app, ["init", "--cloud", "cloudproj"])
    assert first.exit_code == 0, first.output

    other_repo = tmp_path / "other"
    other_repo.mkdir()
    result = runner.invoke(app, ["init", "cloudproj", "--add-repo", str(other_repo)])

    assert result.exit_code == 1
    assert "Cannot --add-repo to cloud-mode project 'cloudproj'" in result.output
    assert f"nauro attach {server_id}" in result.output


# ── add-repo to existing local project still works ────────────────────────────


def test_add_repo_to_local_project_appends(tmp_path, monkeypatch):
    """The legacy --add-repo flow still extends a local-mode project."""
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    repo1 = tmp_path / "repo1"
    repo1.mkdir()
    repo2 = tmp_path / "repo2"
    repo2.mkdir()

    runner.invoke(app, ["init", "extend", "--add-repo", str(repo1)])
    result = runner.invoke(app, ["init", "extend", "--add-repo", str(repo2)])
    assert result.exit_code == 0, result.output

    matches = registry.find_projects_by_name_v2("extend")
    assert len(matches) == 1
    _pid, entry = matches[0]
    paths = entry["repo_paths"]
    assert str(repo1.resolve()) in paths
    assert str(repo2.resolve()) in paths
