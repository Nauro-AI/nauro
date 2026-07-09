"""Tests for `nauro link --cloud`.

``link --cloud`` mints a cloud project, re-keys the local store to it, then
performs the first cloud push in the same invocation so promotion is one
command rather than link-then-sync.

Paths covered:

1. Happy path: local-mode repo is re-keyed to a server-minted ULID and the
   store is pushed to the cloud within the single invocation.
2. Push-failure resilience: a transient presign/upload error warns and exits
   0 with the re-key intact — the irreversible promotion is never rolled back.
3. Already-cloud repo: nothing to link → clear error, no-op.
4. No-config repo: not a nauro repo → clear error.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.config import save_config
from nauro.store.repo_config import load_repo_config, save_repo_config
from nauro.sync import cloud_projects, remote

runner = CliRunner()

CLOUD_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _seed_token(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    save_config(
        {
            "auth": {"access_token": "test-token", "sub": "auth0|test"},
        }
    )


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


def _presign_post(url, **kwargs):
    """Mint a presigned PUT URL per requested operation."""
    operations = kwargs["json"]["operations"]
    return httpx.Response(
        200,
        json={
            "urls": [
                {
                    "verb": op["verb"],
                    "path": op["path"],
                    "url": f"https://s3.example/PUT/{op['path']}",
                    "expires_at": "2026-05-29T13:00:00Z",
                }
                for op in operations
            ]
        },
        request=httpx.Request("POST", url),
    )


def _ok_put():
    put_response = MagicMock(spec=httpx.Response)
    put_response.status_code = 200
    put_response.headers = {"ETag": '"e_pushed"'}
    return put_response


def test_link_cloud_promotes_and_pushes_in_one_command(tmp_path, monkeypatch):
    """The store is re-keyed to the cloud id AND pushed within one invocation."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    init_result = runner.invoke(app, ["init", "linkproj"])
    assert init_result.exit_code == 0, init_result.output

    matches = registry.find_projects_by_name_v2("linkproj")
    assert len(matches) == 1
    local_id, _entry = matches[0]
    local_store = tmp_path / "projects" / local_id
    assert local_store.is_dir()

    # Mark the store with a sentinel so we can prove the rename moved its contents
    sentinel = local_store / "decisions" / "999-sentinel.md"
    sentinel.write_text("# 999 sentinel\n")

    with (
        patch.object(cloud_projects.httpx, "request", side_effect=_create_response()),
        patch.object(remote.httpx, "post", side_effect=_presign_post),
        patch.object(remote.httpx, "put", return_value=_ok_put()) as mock_put,
    ):
        result = runner.invoke(app, ["link", "--cloud"])
    assert result.exit_code == 0, result.output

    # The push ran inside this single invocation: store files were PUT to S3.
    assert mock_put.call_count > 0
    assert "Pushed" in result.output

    new_store = tmp_path / "projects" / CLOUD_PID
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


def test_link_cloud_push_failure_keeps_rekey(tmp_path, monkeypatch):
    """A transient push failure warns + exits 0; the promotion persists."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    init_result = runner.invoke(app, ["init", "linkproj"])
    assert init_result.exit_code == 0, init_result.output

    matches = registry.find_projects_by_name_v2("linkproj")
    local_id, _entry = matches[0]

    transient = remote.PresignError("POST /sync/presign failed (503): unavailable")

    with (
        patch.object(cloud_projects.httpx, "request", side_effect=_create_response()),
        patch.object(remote, "request_presigned_urls", side_effect=transient),
    ):
        result = runner.invoke(app, ["link", "--cloud"])

    # Push failed, but promotion is irreversible — exit 0, warn naming sync.
    assert result.exit_code == 0, result.output
    assert "nauro sync" in result.output

    # The re-key persisted despite the failed push.
    assert registry.get_project_v2(local_id) is None
    new_entry = registry.get_project_v2(CLOUD_PID)
    assert new_entry is not None
    assert new_entry["mode"] == "cloud"

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


def test_link_cloud_succeeds_with_only_auth_token(tmp_path, monkeypatch):
    """An Auth0 token alone is enough to link — static IAM creds are not required."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    init_result = runner.invoke(app, ["init", "auth-only-link"])
    assert init_result.exit_code == 0, init_result.output

    with (
        patch.object(
            cloud_projects.httpx, "request", side_effect=_create_response("auth-only-link")
        ),
        patch.object(remote.httpx, "post", side_effect=_presign_post),
        patch.object(remote.httpx, "put", return_value=_ok_put()),
    ):
        result = runner.invoke(app, ["link", "--cloud"])

    assert result.exit_code == 0, result.output
    new_entry = registry.get_project_v2(CLOUD_PID)
    assert new_entry is not None
    assert new_entry["mode"] == "cloud"


@pytest.mark.parametrize(
    ("track_config", "expected"),
    [
        (False, ".nauro/config.json is untracked and not git-ignored"),
        (True, ".nauro/config.json is tracked by git"),
    ],
)
def test_link_cloud_warns_for_repo_config_git_hygiene(
    tmp_path, monkeypatch, track_config, expected
):
    _seed_token(monkeypatch, tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    init_result = runner.invoke(app, ["init", "linkproj"])
    assert init_result.exit_code == 0, init_result.output
    if track_config:
        subprocess.run(["git", "add", ".nauro/config.json"], cwd=tmp_path, check=True)

    with (
        patch.object(cloud_projects.httpx, "request", side_effect=_create_response()),
        patch.object(remote.httpx, "post", side_effect=_presign_post),
        patch.object(remote.httpx, "put", return_value=_ok_put()),
    ):
        result = runner.invoke(app, ["link", "--cloud"])

    assert result.exit_code == 0, result.output
    assert expected in result.output
    assert "repo-local Nauro project config" in result.output


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
