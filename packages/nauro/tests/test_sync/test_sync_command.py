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
            return True

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


class TestSyncPreservesState:
    """Regression: sync must not write snapshot labels into state files."""

    RICH_STATE = "Sprint 5: shipping feature X.\nBlockers: none.\nNext: write release notes."

    def _seed_rich_state(self, store):
        from nauro.store.writer import update_state

        update_state(store, self.RICH_STATE)

    def _read_state_files(self, store):
        from nauro.constants import STATE_CURRENT_FILENAME, STATE_HISTORY_FILENAME

        current = (store / STATE_CURRENT_FILENAME).read_text()
        history_path = store / STATE_HISTORY_FILENAME
        history = history_path.read_text() if history_path.exists() else ""
        return current, history

    def test_rich_state_survives_repeated_sync(self, project_store):
        """Repeated `nauro sync` must leave rich state_current.md intact and keep
        snapshot labels out of state_history.md."""
        from nauro.store.snapshot import list_snapshots

        self._seed_rich_state(project_store)
        baseline_snapshots = len(list_snapshots(project_store))

        for _ in range(3):
            result = runner.invoke(app, ["sync"])
            assert result.exit_code == 0, result.output

        current, history = self._read_state_files(project_store)

        assert "Sprint 5: shipping feature X." in current
        assert "Blockers: none." in current
        assert "Snapshot v" not in current
        assert "manual sync" not in current
        assert "Snapshot v" not in history
        assert "manual sync" not in history

        snapshots = list_snapshots(project_store)
        assert len(snapshots) - baseline_snapshots == 3
        assert snapshots[0]["trigger"] == "manual sync"

    def test_custom_message_routes_to_snapshot_only(self, project_store):
        """`-m <msg>` must land in snapshot metadata, never in state files."""
        from nauro.store.snapshot import list_snapshots

        self._seed_rich_state(project_store)

        result = runner.invoke(app, ["sync", "-m", "release-prep"])
        assert result.exit_code == 0, result.output

        current, history = self._read_state_files(project_store)

        assert "Sprint 5: shipping feature X." in current
        assert "release-prep" not in current
        assert "release-prep" not in history

        snapshots = list_snapshots(project_store)
        assert snapshots[0]["trigger"] == "release-prep"

    def test_legitimate_state_rotation_still_works(self, project_store):
        """Sanity check: update_state() calls between syncs still archive prior
        state into state_history.md — sync just stops doing this itself."""
        from nauro.store.writer import update_state

        self._seed_rich_state(project_store)

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output

        update_state(project_store, "Sprint 6: new sprint.")

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output

        current, history = self._read_state_files(project_store)

        assert "Sprint 6: new sprint." in current
        assert "Sprint 5: shipping feature X." in history
        assert "Snapshot v" not in history


def _scaffolded_cloud_project(name: str, repo_path: Path):
    """Register a cloud-mode v2 project and scaffold its store. Returns the store path."""
    from nauro.constants import REPO_CONFIG_MODE_CLOUD
    from nauro.store.registry import register_project_v2
    from nauro.templates.scaffolds import scaffold_project_store

    _pid, store = register_project_v2(
        name,
        [repo_path],
        mode=REPO_CONFIG_MODE_CLOUD,
        server_url="https://example.test",
    )
    scaffold_project_store(name, store)
    return store


class TestSyncHonesty:
    """sync() must not print 'Synced' unless an upload actually happened (or
    none was expected). The four cases below cover the matrix:

    cloud-mode + disabled creds → warn on stderr, exit 1
    local-mode + disabled creds → Synced, exit 0
    cloud-mode + enabled creds  → Synced, exit 0
    """

    def test_cloud_project_without_creds_warns_and_exits_one(self, tmp_path, monkeypatch):
        _scaffolded_cloud_project("cloudproj", tmp_path)
        # NAURO_HOME isolation in conftest already prevents real creds from leaking.
        monkeypatch.delenv("NAURO_SYNC_BUCKET_NAME", raising=False)

        result = runner.invoke(app, ["sync", "--project", "cloudproj"])
        combined = result.output + (result.stderr or "")

        assert result.exit_code == 1, combined
        assert "Warning: this is a cloud-mode project" in combined
        assert "Synced cloudproj" not in result.output

    def test_local_project_without_creds_succeeds(self, project_store):
        """Local-only projects sync cleanly even with no cloud creds — nothing
        to upload is not an error."""
        result = runner.invoke(app, ["sync"])
        combined = result.output + (result.stderr or "")

        assert result.exit_code == 0, combined
        assert "Synced testproj" in result.output
        assert "Warning: this is a cloud-mode project" not in combined

    def test_cloud_project_with_creds_succeeds(self, tmp_path, monkeypatch):
        """With creds configured and the push helpers mocked to succeed, the
        cloud-mode project syncs and reports success."""
        from nauro.sync.config import SyncConfig

        _scaffolded_cloud_project("cloudwithauth", tmp_path)

        enabled = SyncConfig(
            bucket_name="bucket-x",
            region="us-east-1",
            access_key_id="key",
            secret_access_key="secret",
            enabled=True,
            sanitized_sub="test-sub",
        )

        with (
            patch("nauro.sync.config.load_sync_config", return_value=enabled),
            patch("nauro.sync.remote.create_client", return_value=MagicMock()),
            patch("nauro.sync.remote.push_file", return_value='"etag"'),
            patch("nauro.sync.remote.list_remote", return_value=[]),
        ):
            result = runner.invoke(app, ["sync", "--project", "cloudwithauth"])

        combined = result.output + (result.stderr or "")
        assert result.exit_code == 0, combined
        assert "Synced cloudwithauth" in result.output
        assert "Warning: this is a cloud-mode project" not in combined


class TestLinkCloudRefusesWithoutCreds:
    """`nauro link --cloud` must refuse when CLI sync credentials are missing
    instead of minting a cloud id the user cannot upload to."""

    def test_link_cloud_refuses_when_sync_disabled(self, tmp_path, monkeypatch):
        """A local-mode repo + no sync creds → refusal, no network call."""
        from nauro.store.config import save_config

        # Bring up a local-only project from scratch in the isolated NAURO_HOME.
        save_config({"auth": {"access_token": "test-token", "sub": "auth0|test"}})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NAURO_API_URL", "https://example.test")

        init_result = runner.invoke(app, ["init", "blockedlink"])
        assert init_result.exit_code == 0, init_result.output

        # Ensure no sync creds leak in from the environment.
        for var in (
            "NAURO_SYNC_BUCKET_NAME",
            "NAURO_SYNC_ACCESS_KEY_ID",
            "NAURO_SYNC_SECRET_ACCESS_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

        result = runner.invoke(app, ["link", "--cloud"])
        combined = result.output + (result.stderr or "")

        assert result.exit_code == 1, combined
        assert "Cannot link 'blockedlink' to the cloud" in combined
