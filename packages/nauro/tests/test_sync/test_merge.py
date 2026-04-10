"""Tests for nauro.sync.merge."""

import shutil

import pytest

from nauro.sync.merge import (
    _is_append_only,
    detect_conflict,
    resolve_conflict,
    should_skip,
)
from nauro.sync.state import FileState, SyncState


class TestShouldSkip:
    def test_sync_state_file(self):
        assert should_skip(".sync-state.json") is True

    def test_normal_file(self):
        assert should_skip("project.md") is False

    def test_decision_file(self):
        assert should_skip("decisions/001-foo.md") is False


class TestIsAppendOnly:
    def test_decision_file(self):
        assert _is_append_only("decisions/001-foo.md") is True

    def test_open_questions(self):
        assert _is_append_only("open-questions.md") is True

    def test_project_md(self):
        assert _is_append_only("project.md") is False

    def test_state_md(self):
        assert _is_append_only("state.md") is False

    def test_state_current_md(self):
        assert _is_append_only("state_current.md") is False

    def test_state_history_md(self):
        assert _is_append_only("state_history.md") is True


class TestDetectConflict:
    def test_no_previous_state(self):
        state = SyncState()
        assert detect_conflict("new.md", state, "sha1", '"etag1"') is False

    def test_only_local_changed(self):
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256="old_sha", remote_etag='"same_etag"')
        assert detect_conflict("test.md", state, "new_sha", '"same_etag"') is False

    def test_only_remote_changed(self):
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256="same_sha", remote_etag='"old_etag"')
        assert detect_conflict("test.md", state, "same_sha", '"new_etag"') is False

    def test_both_changed(self):
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256="old_sha", remote_etag='"old_etag"')
        assert detect_conflict("test.md", state, "new_sha", '"new_etag"') is True


class TestResolveConflict:
    def test_lww_for_state_md(self, tmp_path):
        """state.md uses last-write-wins with backup."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "state.md"
        local_file.write_text("local state content")
        remote_content = b"remote state content"

        state = SyncState()
        result = resolve_conflict(project_path, local_file, remote_content, "state.md", state)

        assert result == b"local state content"
        backup_dir = project_path / ".conflict-backup"
        assert backup_dir.exists()
        backups = list(backup_dir.iterdir())
        assert len(backups) == 1
        assert backups[0].read_bytes() == remote_content

    def test_lww_for_project_md(self, tmp_path):
        """project.md uses last-write-wins with backup."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "project.md"
        local_file.write_text("local project content")
        remote_content = b"remote project content"

        state = SyncState()
        result = resolve_conflict(project_path, local_file, remote_content, "project.md", state)

        assert result == b"local project content"
        backup_dir = project_path / ".conflict-backup"
        assert backup_dir.exists()

    def test_lww_for_snapshots(self, tmp_path):
        """Snapshot files use last-write-wins."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "snapshots").mkdir()
        local_file = project_path / "snapshots" / "v001.json"
        local_file.write_text('{"local": true}')
        remote_content = b'{"remote": true}'

        state = SyncState()
        result = resolve_conflict(
            project_path, local_file, remote_content, "snapshots/v001.json", state
        )

        assert result == b'{"local": true}'

    @pytest.mark.skipif(not shutil.which("git"), reason="git not available")
    def test_union_merge_for_decisions(self, tmp_path):
        """Decision files use git merge-file --union."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "decisions").mkdir()
        local_file = project_path / "decisions" / "001-foo.md"
        local_file.write_text("# Decision 001\nLocal addition\n")
        remote_content = b"# Decision 001\nRemote addition\n"

        state = SyncState()
        result = resolve_conflict(
            project_path, local_file, remote_content, "decisions/001-foo.md", state
        )

        # Union merge should include content from both sides
        result_str = result.decode()
        assert "Decision 001" in result_str

    @pytest.mark.skipif(not shutil.which("git"), reason="git not available")
    def test_union_merge_for_open_questions(self, tmp_path):
        """open-questions.md uses git merge-file --union."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "open-questions.md"
        local_file.write_text("- Question 1\n- Local question\n")
        remote_content = b"- Question 1\n- Remote question\n"

        state = SyncState()
        result = resolve_conflict(
            project_path, local_file, remote_content, "open-questions.md", state
        )

        result_str = result.decode()
        assert "Question 1" in result_str
