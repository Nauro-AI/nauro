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

from nauro.store.config import save_config
from nauro.store.registry import register_project, register_project_v2
from nauro.sync.hooks import (
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
from tests.conftest import seed_auth_config
from tests.test_sync.conftest import CLOUD_PID, _scaffolded_cloud_project


def _ok(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _seed_token() -> None:
    seed_auth_config(variant="sync")


# --- gating: silent no-op semantics ---


class TestSilentNoOpGating:
    """The hooks must silent-no-op when not authenticated or when the
    project is not v2 cloud-mode. Nagging the user on every MCP write
    would be hostile; v1/local projects have no presign target."""

    def test_pull_silent_when_not_authenticated(self, tmp_path):
        store = _scaffolded_cloud_project("noauth", tmp_path, project_id=CLOUD_PID)
        # no _seed_token() — load_access_token() returns None

        with patch("nauro.sync.remote.httpx.get") as mock_get:
            result = pull_before_session(CLOUD_PID, store)

        assert result == 0
        mock_get.assert_not_called()

    def test_push_silent_when_not_authenticated(self, tmp_path):
        store = _scaffolded_cloud_project("noauth", tmp_path, project_id=CLOUD_PID)
        # no _seed_token()

        with patch("nauro.sync.remote.httpx.post") as mock_post:
            result = push_after_write(CLOUD_PID, store)

        assert result == 0
        mock_post.assert_not_called()

    def test_pull_silent_for_v1_project(self, tmp_path):
        """v1 entries have no v2 registry record → is_cloud_project False."""
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


# --- pull happy path ---


class TestPullBeforeSessionPresign:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pullproj", tmp_path, project_id=CLOUD_PID)
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

    def test_pull_swallows_union_merge_error_and_leaves_file_untouched(
        self, cloud_store, monkeypatch
    ):
        """SessionStart auto-pull must not crash on a failed union merge.

        The failing file is skipped (left on disk as-is), the rest of the
        pull continues, and the call returns normally.
        """
        from nauro.sync.merge import UnionMergeError

        rel = "decisions/050-conflicted.md"
        local_file = cloud_store / rel
        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_bytes(b"# 050\nlocal body\n")
        original = local_file.read_bytes()

        # Diverged from last-synced state on both sides → conflict, not pull.
        state = SyncState()
        state.files[rel] = FileState(
            local_sha256="old_sha",
            remote_etag='"old_etag"',
            last_sync="2026-05-16T00:00:00Z",
        )
        save_state(cloud_store, state)

        manifest = self._manifest(
            [{"path": rel, "etag": '"new_etag"', "size": 1, "last_modified": "x"}],
        )
        presign = self._presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"# 050\nremote body\n")

        def boom(*args, **kwargs):
            raise UnionMergeError("simulated git failure")

        monkeypatch.setattr("nauro.sync.pull.resolve_conflict", boom)

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            # Must not raise.
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 0
        # The local file was left untouched rather than overwritten.
        assert local_file.read_bytes() == original


# --- push happy path ---


class TestPushAfterWritePresign:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pushproj", tmp_path, project_id=CLOUD_PID)
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

    def test_push_after_write_delegates_to_push_changed_files(self, cloud_store):
        """The hook is a thin wrapper over the shared push module — it must
        route the same changed-file set through ``push_changed_files`` exactly
        once (retiring the old inline copy and its double-hash)."""
        self._seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("changed\n")

        with patch("nauro.sync.push.push_changed_files", return_value=2) as mock_push:
            result = push_after_write(CLOUD_PID, cloud_store)

        assert result == 2
        assert mock_push.call_count == 1
        assert mock_push.call_args.args == (CLOUD_PID, cloud_store)


# --- hook never-raise envelope ---


class TestPullBeforeSessionNeverRaises:
    """``pull_before_session`` must never propagate an exception and must stay
    silent on stdout even when the shared core's dependencies blow up."""

    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("silentpull", tmp_path, project_id=CLOUD_PID)
        _seed_token()
        return store

    def test_returns_zero_and_silent_when_run_pull_raises(self, cloud_store, capsys):
        with patch("nauro.sync.pull.run_pull", side_effect=RuntimeError("unexpected boom")):
            result = pull_before_session(CLOUD_PID, cloud_store)

        assert result == 0
        captured = capsys.readouterr()
        assert captured.out == ""
