"""Tests for nauro.sync.state."""

import pytest

from nauro.sync.state import (
    FileState,
    SyncState,
    compute_sha256,
    file_changed_locally,
    file_changed_remotely,
    load_state,
    save_state,
    update_file_state,
)


@pytest.fixture
def project_path(tmp_path):
    return tmp_path / "project"


@pytest.fixture
def project_dir(project_path):
    project_path.mkdir()
    return project_path


class TestLoadSaveState:
    def test_load_nonexistent(self, project_dir):
        state = load_state(project_dir)
        assert state.files == {}
        assert state.last_full_sync == ""

    def test_round_trip(self, project_dir):
        state = SyncState(last_full_sync="2026-03-17T10:00:00Z")
        state.files["project.md"] = FileState(
            local_sha256="abc123",
            remote_etag='"def456"',
            last_sync="2026-03-17T10:00:00Z",
        )
        save_state(project_dir, state)
        loaded = load_state(project_dir)
        assert loaded.last_full_sync == "2026-03-17T10:00:00Z"
        assert "project.md" in loaded.files
        assert loaded.files["project.md"].local_sha256 == "abc123"
        assert loaded.files["project.md"].remote_etag == '"def456"'

    def test_corrupt_json(self, project_dir):
        (project_dir / ".sync-state.json").write_text("not json{{{")
        state = load_state(project_dir)
        assert state.files == {}


class TestChangeDetection:
    def test_file_changed_locally_new_file(self, project_dir):
        (project_dir / "new.md").write_text("hello")
        state = SyncState()
        assert file_changed_locally(project_dir, "new.md", state) is True

    def test_file_changed_locally_unchanged(self, project_dir):
        f = project_dir / "test.md"
        f.write_text("content")
        sha = compute_sha256(f)
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256=sha)
        assert file_changed_locally(project_dir, "test.md", state) is False

    def test_file_changed_locally_modified(self, project_dir):
        f = project_dir / "test.md"
        f.write_text("original")
        sha = compute_sha256(f)
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256=sha)
        f.write_text("modified")
        assert file_changed_locally(project_dir, "test.md", state) is True

    def test_file_changed_locally_deleted(self, project_dir):
        state = SyncState()
        state.files["gone.md"] = FileState(local_sha256="abc")
        assert file_changed_locally(project_dir, "gone.md", state) is True

    def test_file_changed_remotely_new(self):
        state = SyncState()
        assert file_changed_remotely('"newtag"', "new.md", state) is True

    def test_file_changed_remotely_unchanged(self):
        state = SyncState()
        state.files["test.md"] = FileState(remote_etag='"tag1"')
        assert file_changed_remotely('"tag1"', "test.md", state) is False

    def test_file_changed_remotely_modified(self):
        state = SyncState()
        state.files["test.md"] = FileState(remote_etag='"tag1"')
        assert file_changed_remotely('"tag2"', "test.md", state) is True


class TestUpdateFileState:
    def test_update_new_entry(self):
        state = SyncState()
        update_file_state(state, "project.md", "sha123", '"etag456"')
        assert "project.md" in state.files
        assert state.files["project.md"].local_sha256 == "sha123"
        assert state.files["project.md"].remote_etag == '"etag456"'
        assert state.files["project.md"].last_sync != ""

    def test_update_existing_entry(self):
        state = SyncState()
        state.files["project.md"] = FileState(
            local_sha256="old", remote_etag='"old"', last_sync="old"
        )
        update_file_state(state, "project.md", "new_sha", '"new_etag"')
        assert state.files["project.md"].local_sha256 == "new_sha"
        assert state.files["project.md"].remote_etag == '"new_etag"'


class TestComputeSha256:
    def test_deterministic(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        sha1 = compute_sha256(f)
        sha2 = compute_sha256(f)
        assert sha1 == sha2
        assert len(sha1) == 64  # hex digest length

    def test_different_content(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        assert compute_sha256(f1) != compute_sha256(f2)
