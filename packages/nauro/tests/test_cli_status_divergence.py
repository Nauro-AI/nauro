"""Tests for status command decision count and sync divergence output."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _setup_project(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(tmp_path)
    return store


def test_status_shows_local_decision_count(tmp_path, monkeypatch):
    """When sync is not configured, show local count only."""
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # scaffold creates 001-initial-setup.md
    assert "Decisions: 1 local" in result.output
    assert "remote" not in result.output


def test_status_shows_divergence_when_out_of_sync(tmp_path, monkeypatch):
    """When sync is configured and counts differ, show 'out of sync'."""
    store = _setup_project(tmp_path, monkeypatch)

    # Write sync state with a last_full_sync timestamp
    sync_state = {
        "files": {},
        "last_full_sync": "2026-03-23T18:42:00+00:00",
    }
    (store / ".sync-state.json").write_text(json.dumps(sync_state))

    mock_config = type(
        "SyncConfig",
        (),
        {
            "enabled": True,
            "bucket_name": "test-bucket",
            "region": "us-east-1",
            "access_key_id": "key",
            "secret_access_key": "secret",
        },
    )()

    with patch("nauro.sync.config.load_sync_config", return_value=mock_config):
        # Mock _count_remote_decisions to return a different count
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
    """When sync is configured and counts match, show 'in sync'."""
    store = _setup_project(tmp_path, monkeypatch)

    sync_state = {
        "files": {},
        "last_full_sync": "2026-03-30T10:00:00+00:00",
    }
    (store / ".sync-state.json").write_text(json.dumps(sync_state))

    mock_config = type(
        "SyncConfig",
        (),
        {
            "enabled": True,
            "bucket_name": "test-bucket",
            "region": "us-east-1",
            "access_key_id": "key",
            "secret_access_key": "secret",
        },
    )()

    with patch("nauro.sync.config.load_sync_config", return_value=mock_config):
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
    """When remote is unreachable, show local count with a note."""
    _setup_project(tmp_path, monkeypatch)

    mock_config = type(
        "SyncConfig",
        (),
        {
            "enabled": True,
            "bucket_name": "test-bucket",
            "region": "us-east-1",
            "access_key_id": "key",
            "secret_access_key": "secret",
        },
    )()

    with patch("nauro.sync.config.load_sync_config", return_value=mock_config):
        with patch(
            "nauro.cli.commands.status._count_remote_decisions",
            return_value=None,
        ):
            result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "1 local" in result.output
    assert "could not reach remote" in result.output
