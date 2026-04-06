"""Tests for event-driven sync hooks (pull on session start, push after extraction)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nauro.store.registry import register_project
from nauro.sync.config import SyncConfig
from nauro.sync.hooks import (
    _renumber_decision_if_collision,
    pull_before_session,
    push_after_extraction,
)
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture()
def project_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    return store


def _mock_sync_config(enabled=True, sanitized_sub="test-user-abc123"):
    return SyncConfig(
        bucket_name="test-bucket",
        region="us-east-1",
        access_key_id="fake",
        secret_access_key="fake",
        enabled=enabled,
        sanitized_sub=sanitized_sub,
    )


class TestPullBeforeSession:
    def test_pull_from_s3_before_returning_context(self, project_store):
        """SessionStart hook should pull from S3."""
        mock_client = MagicMock()
        mock_client.get_paginator.return_value.paginate.return_value = [{"Contents": []}]

        with (
            patch("nauro.sync.config.load_sync_config", return_value=_mock_sync_config()),
            patch("nauro.sync.remote.create_client", return_value=mock_client),
        ):
            result = pull_before_session("testproj", project_store)

        assert result == 0  # No remote changes
        # Verify list_remote was called (via paginator)
        mock_client.get_paginator.assert_called_once_with("list_objects_v2")

    def test_pull_failure_is_non_blocking(self, project_store):
        """If S3 pull fails, should return 0 and not raise."""
        with (
            patch("nauro.sync.config.load_sync_config", return_value=_mock_sync_config()),
            patch("nauro.sync.remote.create_client", side_effect=Exception("network error")),
        ):
            result = pull_before_session("testproj", project_store)

        assert result == 0

    def test_pull_skips_when_not_configured(self, project_store):
        """If sync is not configured, pull should be a no-op."""
        config = _mock_sync_config(enabled=False)
        with patch("nauro.sync.config.load_sync_config", return_value=config):
            result = pull_before_session("testproj", project_store)

        assert result == 0

    def test_pull_skips_when_auth_missing(self, project_store):
        """If sanitized_sub is missing, pull should return 0."""
        config = _mock_sync_config(enabled=True, sanitized_sub="")
        with patch("nauro.sync.config.load_sync_config", return_value=config):
            result = pull_before_session("testproj", project_store)

        assert result == 0


class TestPushAfterExtraction:
    def test_push_to_s3_after_writing_decision(self, project_store):
        """Post-extraction hook should push to S3."""
        mock_client = MagicMock()
        mock_client.put_object.return_value = {"ETag": '"newetag"'}

        with (
            patch("nauro.sync.config.load_sync_config", return_value=_mock_sync_config()),
            patch("nauro.sync.remote.create_client", return_value=mock_client),
        ):
            result = push_after_extraction("testproj", project_store)

        assert result > 0  # Should push store files
        mock_client.put_object.assert_called()

    def test_push_failure_is_non_blocking(self, project_store):
        """If S3 push fails, should return 0 and not raise."""
        with (
            patch("nauro.sync.config.load_sync_config", return_value=_mock_sync_config()),
            patch("nauro.sync.remote.create_client", side_effect=Exception("network error")),
        ):
            result = push_after_extraction("testproj", project_store)

        assert result == 0

    def test_push_skips_when_not_configured(self, project_store):
        """If sync is not configured, push should be a no-op."""
        config = _mock_sync_config(enabled=False)
        with patch("nauro.sync.config.load_sync_config", return_value=config):
            result = push_after_extraction("testproj", project_store)

        assert result == 0

    def test_push_skips_when_auth_missing(self, project_store):
        """If sanitized_sub is missing, push should return 0."""
        config = _mock_sync_config(enabled=True, sanitized_sub="")
        with patch("nauro.sync.config.load_sync_config", return_value=config):
            result = push_after_extraction("testproj", project_store)

        assert result == 0


class TestRenumberDecisionIfCollision:
    def test_no_collision_passes_through(self, project_store):
        """When no collision exists, rel and content are returned unchanged."""
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "001-existing.md").write_text("# 001 — Existing")

        content = b"# 002 \xe2\x80\x94 New decision\n\nSome content"
        rel, out = _renumber_decision_if_collision(project_store, "decisions/002-new.md", content)

        assert rel == "decisions/002-new.md"
        assert out == content

    def test_collision_renumbers(self, project_store):
        """When number collides with a different local file, incoming file is renumbered."""
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "003-local-decision.md").write_text("# 003 — Local decision")

        content = b"# 003 \xe2\x80\x94 Remote decision\n\nRemote content"
        rel, out = _renumber_decision_if_collision(
            project_store, "decisions/003-remote-decision.md", content,
        )

        assert rel == "decisions/004-remote-decision.md"
        assert b"# 004 " in out
        assert b"Remote content" in out

    def test_collision_skips_multiple_taken_numbers(self, project_store):
        """Renumbering should find the first available number after all existing ones."""
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "005-a.md").write_text("# 005 — A")
        (decisions_dir / "006-b.md").write_text("# 006 — B")

        content = b"# 005 \xe2\x80\x94 Incoming\n\nContent"
        rel, out = _renumber_decision_if_collision(
            project_store, "decisions/005-incoming.md", content,
        )

        assert rel == "decisions/007-incoming.md"
        assert b"# 007 " in out

    def test_exact_filename_match_is_not_collision(self, project_store):
        """If the exact same filename exists locally, it's a content update, not a collision."""
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "003-same-slug.md").write_text("# 003 — Same slug")

        content = b"# 003 \xe2\x80\x94 Same slug\n\nUpdated content"
        rel, out = _renumber_decision_if_collision(
            project_store, "decisions/003-same-slug.md", content,
        )

        assert rel == "decisions/003-same-slug.md"
        assert out == content

    def test_non_decision_files_pass_through(self, project_store):
        """Non-decision files should never be renumbered."""
        content = b"some content"
        rel, out = _renumber_decision_if_collision(project_store, "state.md", content)

        assert rel == "state.md"
        assert out == content
