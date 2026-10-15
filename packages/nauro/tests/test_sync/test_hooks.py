"""Tests for event-driven sync hooks (pull on session start, push after write).

After the cutover to presign, ``hooks.py`` gates on:

* Auth0 access token (no token → silent no-op so MCP writes never nag).
* v2 cloud-mode registry entry (unknown id or local-mode → silent no-op).

The happy paths exercise the manifest/presign helpers via httpx mocks
matching the patterns in ``test_sync_presign.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.sync.hooks import (
    _LoggingReporter,
    pull_before_session,
    push_after_write,
)
from nauro.sync.push import PushReport
from nauro.sync.state import load_state
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_sync.conftest import (
    CLOUD_PID,
    _manifest,
    _presign,
    _scaffolded_cloud_project,
    _seed_token,
    patched_remote_get,
    seed_synced_state,
)

# --- gating: silent no-op semantics ---


class TestSilentNoOpGating:
    """The hooks must silent-no-op when not authenticated or when the
    project is not v2 cloud-mode. Nagging the user on every MCP write
    would be hostile; unknown ids and local projects have no presign
    target."""

    def test_pull_silent_when_not_authenticated(self, tmp_path):
        store = _scaffolded_cloud_project("noauth", tmp_path, project_id=CLOUD_PID)
        # no _seed_token() — load_access_token() returns None

        with patch("nauro.sync.remote.httpx.Client.get") as mock_get:
            result = pull_before_session(CLOUD_PID, store)

        assert result == 0
        mock_get.assert_not_called()

    def test_push_silent_when_not_authenticated(self, tmp_path):
        store = _scaffolded_cloud_project("noauth", tmp_path, project_id=CLOUD_PID)
        # no _seed_token()

        with patch("nauro.sync.remote.httpx.Client.post") as mock_post:
            result = push_after_write(CLOUD_PID, store)

        assert result == PushReport()
        mock_post.assert_not_called()

    def _seed_v1_shaped_registry(self, tmp_path) -> Path:
        """Seed the retired v1 (name-keyed) registry shape plus a store dir.

        The shape reads as an empty v2 registry, so ``is_cloud_project``
        is False and the hooks keep their silent no-op.
        """
        (tmp_path / "registry.json").write_text(
            json.dumps(
                {"schema_version": 1, "projects": {"v1proj": {"repo_paths": [str(tmp_path)]}}}
            )
        )
        store = tmp_path / "projects" / "v1proj"
        scaffold_project_store("v1proj", store)
        return store

    def test_pull_silent_for_v1_shaped_registry(self, tmp_path):
        """A v1-shaped registry has no v2 record → is_cloud_project False."""
        store = self._seed_v1_shaped_registry(tmp_path)
        _seed_token()

        with patch("nauro.sync.remote.httpx.Client.get") as mock_get:
            result = pull_before_session("v1proj", store)

        assert result == 0
        mock_get.assert_not_called()

    def test_push_silent_for_v1_shaped_registry(self, tmp_path):
        store = self._seed_v1_shaped_registry(tmp_path)
        _seed_token()

        with patch("nauro.sync.remote.httpx.Client.post") as mock_post:
            result = push_after_write("v1proj", store)

        assert result == PushReport()
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

        with patch("nauro.sync.remote.httpx.Client.get") as mock_get:
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

        with patch("nauro.sync.remote.httpx.Client.post") as mock_post:
            result = push_after_write(local_pid, store)

        assert result == PushReport()
        mock_post.assert_not_called()


# --- pull happy path ---


class TestPullBeforeSessionPresign:
    def test_empty_manifest_returns_zero_and_updates_last_sync(self, cloud_store):
        empty = _manifest([])

        with (
            patch("nauro.sync.remote.httpx.Client.get", return_value=empty),
            patch("nauro.sync.remote.httpx.Client.post") as mock_post,
        ):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 0
        mock_post.assert_not_called()
        state = load_state(cloud_store)
        assert state.last_full_sync != ""

    def test_changed_remote_file_is_fetched_and_written(self, cloud_store):
        rel = "decisions/099-remote.md"
        manifest = _manifest(
            [{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}],
        )
        presign = _presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"# 099\nfresh remote body\n")

        with (
            patched_remote_get(fake_get),
            patch("nauro.sync.remote.httpx.Client.post", return_value=presign),
        ):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 1
        assert (cloud_store / rel).read_bytes() == b"# 099\nfresh remote body\n"
        state = load_state(cloud_store)
        assert state.files[rel].remote_etag == '"new"'

    def test_pull_swallows_presign_error(self, cloud_store):
        """A failed manifest fetch logs and returns 0, never raises."""
        bad_manifest = httpx.Response(500, content=b"server error")
        with patch("nauro.sync.remote.httpx.Client.get", return_value=bad_manifest):
            result = pull_before_session(CLOUD_PID, cloud_store)
        assert result == 0

    def test_pull_swallows_auth_refresh_error(self, cloud_store):
        """A 401 with no refresh path returns 0, never raises."""
        unauthorized = httpx.Response(401, content=b'{"error":"unauthorized"}')
        save_config({"auth": {"access_token": "tok", "sub": "x"}})

        with patch("nauro.sync.remote.httpx.Client.get", return_value=unauthorized):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 0


# --- push happy path ---


class TestPushAfterWritePresign:
    def test_no_local_changes_skips_presign_call(self, cloud_store):
        seed_synced_state(cloud_store)

        with (
            patch("nauro.sync.remote.httpx.Client.post") as mock_post,
            patch("nauro.sync.remote.httpx.Client.put") as mock_put,
        ):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result == PushReport()
        mock_post.assert_not_called()
        mock_put.assert_not_called()

    def test_modified_file_minted_and_uploaded(self, cloud_store):
        seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("new stack picks\n")

        put_response = MagicMock(spec=httpx.Response)
        put_response.status_code = 200
        put_response.headers = {"ETag": '"e_pushed"'}

        def fake_post(url, **kwargs):
            return _presign(kwargs["json"]["operations"])

        with (
            patch("nauro.sync.remote.httpx.Client.post", side_effect=fake_post) as mock_post,
            patch("nauro.sync.remote.httpx.Client.put", return_value=put_response) as mock_put,
        ):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result.verified == ("stack.md",)
        body = mock_post.call_args.kwargs["json"]
        assert body["project_id"] == CLOUD_PID
        assert body["operations"] == [{"verb": "PUT", "path": "stack.md"}]
        assert mock_put.call_count == 1

        state = load_state(cloud_store)
        assert state.files["stack.md"].remote_etag == '"e_pushed"'

    def test_push_swallows_presign_error(self, cloud_store):
        seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("modified\n")

        bad = httpx.Response(500, content=b"server error")
        with patch("nauro.sync.remote.httpx.Client.post", return_value=bad):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result.is_complete is False

    def test_push_swallows_auth_refresh_error(self, cloud_store):
        seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("modified\n")

        unauthorized = httpx.Response(401, content=b'{"error":"unauthorized"}')
        save_config({"auth": {"access_token": "tok", "sub": "x"}})

        with patch("nauro.sync.remote.httpx.Client.post", return_value=unauthorized):
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result.is_complete is False

    def test_push_after_write_delegates_to_push_changed_files(self, cloud_store):
        """The hook is a thin wrapper over the shared push module — it must
        route the same changed-file set through ``push_changed_files`` exactly
        once (retiring the old inline copy and its double-hash)."""
        seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("changed\n")

        report = PushReport(planned=("a", "b"), verified=("a", "b"))
        with patch("nauro.sync.push.push_changed_files", return_value=report) as mock_push:
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result == report
        assert mock_push.call_count == 1
        project_id, store_path, reporter = mock_push.call_args.args
        assert (project_id, store_path) == (CLOUD_PID, cloud_store)
        assert isinstance(reporter, _LoggingReporter)


# --- hook never-raise envelope ---


class TestPullBeforeSessionNeverRaises:
    """``pull_before_session`` must never propagate an exception and must stay
    silent on stdout even when the shared core's dependencies blow up."""

    def test_returns_zero_and_silent_when_run_pull_raises(self, cloud_store, capsys):
        with patch("nauro.sync.pull.run_pull", side_effect=RuntimeError("unexpected boom")):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 0
        captured = capsys.readouterr()
        assert captured.out == ""
