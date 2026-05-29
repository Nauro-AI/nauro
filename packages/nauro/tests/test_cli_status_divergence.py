"""Tests for status command decision count and sync divergence output.

After the auto-sync port, ``sync_enabled`` in ``status`` is gated on:

* An Auth0 access token (via ``load_access_token``).
* A v2 cloud-mode registry entry for the resolved project_id.

Divergence reporting is verified by mocking ``_count_remote_decisions``
directly — the manifest-fetch internals belong to test_sync_presign/test_hooks.
"""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_CLOUD, REPO_CONFIG_MODE_LOCAL
from nauro.store.config import save_config
from nauro.store.registry import register_project, register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

CLOUD_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"
LOCAL_PID = "01KQ6AZGNA0B3QBF67NBXP3S46"


def _setup_v1_project(tmp_path, monkeypatch):
    """v1 project (legacy); status falls back to local-only reporting."""
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(tmp_path)
    return store


def _setup_cloud_project(tmp_path, monkeypatch):
    """v2 cloud-mode project, with an auth token saved."""
    _pid, store = register_project_v2(
        "testproj",
        [tmp_path],
        mode=REPO_CONFIG_MODE_CLOUD,
        project_id=CLOUD_PID,
        server_url="https://example.test",
    )
    scaffold_project_store("testproj", store)
    save_config(
        {
            "auth": {
                "sub": "auth0|test",
                "access_token": "tok_orig",
                "refresh_token": "refresh_orig",
            }
        }
    )
    monkeypatch.chdir(tmp_path)
    return store


def test_status_shows_local_decision_count_when_unauthenticated(tmp_path, monkeypatch):
    """Sync inactive (v1 project, no token) → local count only."""
    _setup_v1_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Decisions: 1 local" in result.output
    assert "remote" not in result.output


def test_status_shows_local_only_when_project_is_local_mode(tmp_path, monkeypatch):
    """v2 local-mode + token → sync inactive, local-only message."""
    _pid, store = register_project_v2(
        "localproj",
        [tmp_path],
        mode=REPO_CONFIG_MODE_LOCAL,
        project_id=LOCAL_PID,
    )
    scaffold_project_store("localproj", store)
    save_config({"auth": {"access_token": "tok", "sub": "x"}})
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "local-only project" in result.output
    assert "nauro link --cloud" in result.output
    assert "remote" not in result.output


def test_status_prompts_login_when_no_token(tmp_path, monkeypatch):
    """No auth token → sync inactive, run nauro auth login."""
    _pid, store = register_project_v2(
        "cloudproj",
        [tmp_path],
        mode=REPO_CONFIG_MODE_CLOUD,
        project_id=CLOUD_PID,
        server_url="https://example.test",
    )
    scaffold_project_store("cloudproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "nauro auth login" in result.output


def test_status_shows_divergence_when_out_of_sync(tmp_path, monkeypatch):
    """Authed cloud project + counts differ → 'out of sync'."""
    store = _setup_cloud_project(tmp_path, monkeypatch)

    sync_state = {
        "files": {},
        "last_full_sync": "2026-03-23T18:42:00+00:00",
    }
    (store / ".sync-state.json").write_text(json.dumps(sync_state))

    with patch(
        "nauro.cli.commands.status._count_remote_decisions",
        return_value=5,
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "1 local" in result.output
    assert "5 remote" in result.output
    assert "out of sync" in result.output
    assert "nauro sync" in result.output
    assert "2026-03-23" in result.output


def test_status_shows_in_sync_when_counts_match(tmp_path, monkeypatch):
    """Authed cloud project + counts match → 'in sync'."""
    store = _setup_cloud_project(tmp_path, monkeypatch)

    sync_state = {
        "files": {},
        "last_full_sync": "2026-03-30T10:00:00+00:00",
    }
    (store / ".sync-state.json").write_text(json.dumps(sync_state))

    with patch(
        "nauro.cli.commands.status._count_remote_decisions",
        return_value=1,
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "1 local" in result.output
    assert "1 remote" in result.output
    assert "in sync" in result.output
    assert "nauro sync" not in result.output.split("in sync")[1]


def test_status_handles_remote_unreachable(tmp_path, monkeypatch):
    """Authed cloud project + manifest fetch fails → 'could not reach remote'."""
    _setup_cloud_project(tmp_path, monkeypatch)

    with patch(
        "nauro.cli.commands.status._count_remote_decisions",
        return_value=None,
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "1 local" in result.output
    assert "could not reach remote" in result.output
