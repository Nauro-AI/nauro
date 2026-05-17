"""Tests for event-driven sync hooks (pull on session start, push after write).

After the cutover to presign, ``hooks.py`` gates on:

* Auth0 access token (no token → silent no-op so MCP writes never nag).
* v2 cloud-mode registry entry (v1 or local-mode → silent no-op).

The happy paths exercise the manifest/presign helpers via httpx mocks
matching the patterns in ``test_sync_presign.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.config import save_config
from nauro.store.registry import register_project, register_project_v2
from nauro.sync.hooks import (
    _project_is_cloud,
    _renumber_decision_if_collision,
    pull_before_session,
    push_after_write,
)
from nauro.sync.state import (
    FileState,
    SyncState,
    compute_sha256,
    load_state,
    save_state,
)
from nauro.templates.scaffolds import scaffold_project_store

CLOUD_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _ok(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _seed_token() -> None:
    save_config(
        {
            "auth": {
                "sub": "auth0|test",
                "access_token": "tok_orig",
                "refresh_token": "refresh_orig",
            }
        }
    )


def _scaffolded_cloud_project(name: str, repo_path: Path, project_id: str = CLOUD_PID) -> Path:
    _pid, store = register_project_v2(
        name,
        [repo_path],
        mode=REPO_CONFIG_MODE_CLOUD,
        server_url="https://example.test",
        project_id=project_id,
    )
    scaffold_project_store(name, store)
    return store


# --- gating: silent no-op semantics ---


class TestSilentNoOpGating:
    """The hooks must silent-no-op when not authenticated or when the
    project is not v2 cloud-mode. Nagging the user on every MCP write
    would be hostile; v1/local projects have no presign target."""

    def test_pull_silent_when_not_authenticated(self, tmp_path):
        store = _scaffolded_cloud_project("noauth", tmp_path)
        # no _seed_token() — load_access_token() returns None

        with patch("nauro.sync.remote.httpx.get") as mock_get:
            result = pull_before_session(CLOUD_PID, store)

        assert result == 0
        mock_get.assert_not_called()

    def test_push_silent_when_not_authenticated(self, tmp_path):
        store = _scaffolded_cloud_project("noauth", tmp_path)
        # no _seed_token()

        with patch("nauro.sync.remote.httpx.post") as mock_post:
            result = push_after_write(CLOUD_PID, store)

        assert result == 0
        mock_post.assert_not_called()

    def test_pull_silent_for_v1_project(self, tmp_path):
        """v1 entries have no v2 registry record → _project_is_cloud False."""
        store = register_project("v1proj", [tmp_path])
        scaffold_project_store("v1proj", store)
        _seed_token()

        with patch("nauro.sync.remote.httpx.get") as mock_get:
            result = pull_before_session("v1proj", store)

        assert result == 0
        mock_get.assert_not_called()

    def test_push_silent_for_v1_project(self, tmp_path):
        store = register_project("v1proj", [tmp_path])
        scaffold_project_store("v1proj", store)
        _seed_token()

        with patch("nauro.sync.remote.httpx.post") as mock_post:
            result = push_after_write("v1proj", store)

        assert result == 0
        mock_post.assert_not_called()

    def test_pull_silent_for_v2_local_mode(self, tmp_path):
        """v2 local-mode projects have no presign target → silent no-op."""
        from nauro.constants import REPO_CONFIG_MODE_LOCAL

        local_pid = "01KQ6AZGNA0B3QBF67NBXP3S46"
        _pid, store = register_project_v2(
            "localproj",
            [tmp_path],
            mode=REPO_CONFIG_MODE_LOCAL,
            project_id=local_pid,
        )
        scaffold_project_store("localproj", store)
        _seed_token()

        with patch("nauro.sync.remote.httpx.get") as mock_get:
            result = pull_before_session(local_pid, store)

        assert result == 0
        mock_get.assert_not_called()

    def test_push_silent_for_v2_local_mode(self, tmp_path):
        from nauro.constants import REPO_CONFIG_MODE_LOCAL

        local_pid = "01KQ6AZGNA0B3QBF67NBXP3S46"
        _pid, store = register_project_v2(
            "localproj",
            [tmp_path],
            mode=REPO_CONFIG_MODE_LOCAL,
            project_id=local_pid,
        )
        scaffold_project_store("localproj", store)
        _seed_token()

        with patch("nauro.sync.remote.httpx.post") as mock_post:
            result = push_after_write(local_pid, store)

        assert result == 0
        mock_post.assert_not_called()


# --- _project_is_cloud helper ---


class TestProjectIsCloud:
    def test_returns_true_for_v2_cloud(self, tmp_path):
        _scaffolded_cloud_project("cloudproj", tmp_path)
        assert _project_is_cloud(CLOUD_PID) is True

    def test_returns_false_for_v2_local(self, tmp_path):
        from nauro.constants import REPO_CONFIG_MODE_LOCAL

        local_pid = "01KQ6AZGNA0B3QBF67NBXP3S46"
        register_project_v2(
            "localproj",
            [tmp_path],
            mode=REPO_CONFIG_MODE_LOCAL,
            project_id=local_pid,
        )
        assert _project_is_cloud(local_pid) is False

    def test_returns_false_for_missing_entry(self):
        assert _project_is_cloud("01KMISSING00000000000000000") is False

    def test_returns_false_for_v1_name(self, tmp_path):
        register_project("v1name", [tmp_path])
        assert _project_is_cloud("v1name") is False


# --- pull happy path ---


class TestPullBeforeSessionPresign:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pullproj", tmp_path)
        _seed_token()
        return store

    def _manifest(self, files, next_cursor=None):
        return _ok(200, {"files": files, "next_cursor": next_cursor})

    def _presign(self, ops):
        return _ok(
            200,
            {
                "urls": [
                    {
                        "verb": op["verb"],
                        "path": op["path"],
                        "url": f"https://s3.example/{op['verb']}/{op['path']}",
                        "expires_at": "2026-05-16T13:00:00Z",
                    }
                    for op in ops
                ]
            },
        )

    def test_empty_manifest_returns_zero_and_updates_last_sync(self, cloud_store):
        empty = self._manifest([])

        with (
            patch("nauro.sync.remote.httpx.get", return_value=empty),
            patch("nauro.sync.remote.httpx.post") as mock_post,
        ):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 0
        mock_post.assert_not_called()
        state = load_state(cloud_store)
        assert state.last_full_sync != ""

    def test_changed_remote_file_is_fetched_and_written(self, cloud_store):
        rel = "decisions/099-remote.md"
        manifest = self._manifest(
            [{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}],
        )
        presign = self._presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"# 099\nfresh remote body\n")

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 1
        assert (cloud_store / rel).read_bytes() == b"# 099\nfresh remote body\n"
        state = load_state(cloud_store)
        assert state.files[rel].remote_etag == '"new"'

    def test_pull_swallows_presign_error(self, cloud_store):
        """A failed manifest fetch logs and returns 0, never raises."""
        bad_manifest = httpx.Response(500, content=b"server error")
        with patch("nauro.sync.remote.httpx.get", return_value=bad_manifest):
            result = pull_before_session(CLOUD_PID, cloud_store)
        assert result == 0

    def test_pull_swallows_auth_refresh_error(self, cloud_store):
        """A 401 with no refresh path returns 0, never raises."""
        unauthorized = httpx.Response(401, content=b'{"error":"unauthorized"}')
        save_config({"auth": {"access_token": "tok", "sub": "x"}})

        with patch("nauro.sync.remote.httpx.get", return_value=unauthorized):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 0


# --- push happy path ---


class TestPushAfterWritePresign:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pushproj", tmp_path)
        _seed_token()
        return store

    def _seed_synced_state(self, store: Path) -> None:
        state = SyncState()
        for f in store.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(store))
            state.files[rel] = FileState(
                local_sha256=compute_sha256(f),
                remote_etag='"e0"',
                last_sync="2026-05-16T00:00:00Z",
            )
        save_state(store, state)

    def _presign(self, ops):
        return _ok(
            200,
            {
                "urls": [
                    {
                        "verb": op["verb"],
                        "path": op["path"],
                        "url": f"https://s3.example/PUT/{op['path']}",
                        "expires_at": "2026-05-16T13:00:00Z",
                    }
                    for op in ops
                ]
            },
        )

    def test_no_local_changes_skips_presign_call(self, cloud_store):
        self._seed_synced_state(cloud_store)

        with (
            patch("nauro.sync.remote.httpx.post") as mock_post,
            patch("nauro.sync.remote.httpx.put") as mock_put,
        ):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result == 0
        mock_post.assert_not_called()
        mock_put.assert_not_called()

    def test_modified_file_minted_and_uploaded(self, cloud_store):
        self._seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("new stack picks\n")

        put_response = MagicMock(spec=httpx.Response)
        put_response.status_code = 200
        put_response.headers = {"ETag": '"e_pushed"'}

        def fake_post(url, **kwargs):
            return self._presign(kwargs["json"]["operations"])

        with (
            patch("nauro.sync.remote.httpx.post", side_effect=fake_post) as mock_post,
            patch("nauro.sync.remote.httpx.put", return_value=put_response) as mock_put,
        ):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result == 1
        body = mock_post.call_args.kwargs["json"]
        assert body["project_id"] == CLOUD_PID
        assert body["operations"] == [{"verb": "PUT", "path": "stack.md"}]
        assert mock_put.call_count == 1

        state = load_state(cloud_store)
        assert state.files["stack.md"].remote_etag == '"e_pushed"'

    def test_push_swallows_presign_error(self, cloud_store):
        self._seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("modified\n")

        bad = httpx.Response(500, content=b"server error")
        with patch("nauro.sync.remote.httpx.post", return_value=bad):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result == 0

    def test_push_swallows_auth_refresh_error(self, cloud_store):
        self._seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("modified\n")

        unauthorized = httpx.Response(401, content=b'{"error":"unauthorized"}')
        save_config({"auth": {"access_token": "tok", "sub": "x"}})

        with patch("nauro.sync.remote.httpx.post", return_value=unauthorized):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result == 0


# --- decision collision renumbering (unchanged from pre-port) ---


class TestRenumberDecisionIfCollision:
    @pytest.fixture()
    def project_store(self, tmp_path):
        store = register_project("renumproj", [tmp_path])
        scaffold_project_store("renumproj", store)
        return store

    def test_no_collision_passes_through(self, project_store):
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "001-existing.md").write_text("# 001 — Existing")

        content = b"# 002 \xe2\x80\x94 New decision\n\nSome content"
        rel, out = _renumber_decision_if_collision(project_store, "decisions/002-new.md", content)

        assert rel == "decisions/002-new.md"
        assert out == content

    def test_collision_renumbers(self, project_store):
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "003-local-decision.md").write_text("# 003 — Local decision")

        content = b"# 003 \xe2\x80\x94 Remote decision\n\nRemote content"
        rel, out = _renumber_decision_if_collision(
            project_store,
            "decisions/003-remote-decision.md",
            content,
        )

        assert rel == "decisions/004-remote-decision.md"
        assert b"# 004 " in out
        assert b"Remote content" in out

    def test_collision_skips_multiple_taken_numbers(self, project_store):
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "005-a.md").write_text("# 005 — A")
        (decisions_dir / "006-b.md").write_text("# 006 — B")

        content = b"# 005 \xe2\x80\x94 Incoming\n\nContent"
        rel, out = _renumber_decision_if_collision(
            project_store,
            "decisions/005-incoming.md",
            content,
        )

        assert rel == "decisions/007-incoming.md"
        assert b"# 007 " in out

    def test_exact_filename_match_is_not_collision(self, project_store):
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "003-same-slug.md").write_text("# 003 — Same slug")

        content = b"# 003 \xe2\x80\x94 Same slug\n\nUpdated content"
        rel, out = _renumber_decision_if_collision(
            project_store,
            "decisions/003-same-slug.md",
            content,
        )

        assert rel == "decisions/003-same-slug.md"
        assert out == content

    def test_non_decision_files_pass_through(self, project_store):
        content = b"some content"
        rel, out = _renumber_decision_if_collision(project_store, "state.md", content)

        assert rel == "state.md"
        assert out == content
