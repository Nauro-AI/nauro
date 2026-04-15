"""Tests for nauro sync bidirectional pull-then-push behavior."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


@pytest.fixture()
def project_store(tmp_path: Path, monkeypatch):
    """Set up a project store for testing."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(tmp_path)
    return store


class TestSyncPullBeforePush:
    """Verify that sync pulls from S3 before pushing."""

    def test_sync_with_s3_calls_pull_before_push(self, project_store, monkeypatch):
        """When S3 is configured, sync should pull then push."""
        call_order = []

        # We need to patch at the module level where they're defined
        from nauro.cli.commands import sync as sync_mod

        def mock_pull(project_name, store_path):
            call_order.append("pull")
            return 0

        def mock_push(project_name, store_path):
            call_order.append("push")

        monkeypatch.setattr(sync_mod, "_pull_from_cloud", mock_pull)
        monkeypatch.setattr(sync_mod, "_push_to_cloud", mock_push)

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert call_order == ["pull", "push"]

    def test_sync_without_s3_unchanged(self, project_store, monkeypatch):
        """When S3 is not configured, sync should still work (pull is a no-op)."""
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "Synced testproj" in result.output
        # No "Pulling from remote" because sync is not configured
        assert "Pulling from remote" not in result.output

    def test_pull_merges_remote_changes(self, project_store, tmp_path, monkeypatch):
        """When remote has changes local doesn't, pull should merge them."""
        from nauro.cli.commands.sync import _pull_from_cloud
        from nauro.sync.config import SyncConfig
        from nauro.sync.state import SyncState, save_state

        # Set up sync state — file NOT in state (new remote file)
        state = SyncState()
        save_state(project_store, state)

        mock_config = SyncConfig(
            bucket_name="test-bucket",
            region="us-east-1",
            access_key_id="fake",
            secret_access_key="fake",
            enabled=True,
            sanitized_sub="test-user-abc123",
        )

        remote_content = b"# Decision 037\nTest decision from remote"
        mock_client = MagicMock()
        mock_client.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {
                        "Key": "users/test-user-abc123/projects/testproj/decisions/037-test.md",
                        "ETag": '"newetag"',
                        "Size": len(remote_content),
                    }
                ]
            }
        ]
        mock_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: remote_content),
            "ETag": '"newetag"',
        }

        # Patch pull_file to write the content
        def fake_pull(client, bucket, key, local_path):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(remote_content)
            return '"newetag"'

        with (
            patch("nauro.sync.config.load_sync_config", return_value=mock_config),
            patch("nauro.sync.remote.create_client", return_value=mock_client),
            patch("nauro.sync.remote.pull_file", side_effect=fake_pull),
        ):
            merged = _pull_from_cloud("testproj", project_store)

        assert merged == 1
        pulled_file = project_store / "decisions" / "037-test.md"
        assert pulled_file.exists()
        assert b"Test decision from remote" in pulled_file.read_bytes()


class TestSyncPullNoConfig:
    """Verify pull is a no-op when S3 is not configured."""

    def test_pull_returns_zero_when_not_configured(self, project_store):
        """_pull_from_cloud should return 0 when sync is not configured."""
        from nauro.cli.commands.sync import _pull_from_cloud

        result = _pull_from_cloud("testproj", project_store)
        assert result == 0
