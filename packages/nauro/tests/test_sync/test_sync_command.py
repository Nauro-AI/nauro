"""Tests for nauro sync bidirectional pull-then-push behavior."""

from pathlib import Path
from unittest.mock import patch

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
        assert "local-only project; nothing to upload" in result.output
        # No "Pulling from remote" because sync is not configured
        assert "Pulling from remote" not in result.output


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
        from nauro_core.operations import update_state as _update_state_op

        from nauro.store.filesystem_store import FilesystemStore

        def update_state(store_path, delta):
            _update_state_op(FilesystemStore(store_path), delta)

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
        from nauro_core.operations import update_state as _update_state_op

        from nauro.store.filesystem_store import FilesystemStore

        def update_state(store_path, delta):
            _update_state_op(FilesystemStore(store_path), delta)

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
    """sync() must not print 'Synced' unless an actual cloud upload happened.
    The three cases below cover the matrix:

    cloud-mode + disabled creds → warn on stderr, exit 1
    local-mode + no creds       → honest local-only message, exit 0
    cloud-mode + enabled creds  → Synced, exit 0
    """

    def test_cloud_project_without_auth_warns_and_exits_one(self, tmp_path, monkeypatch):
        _scaffolded_cloud_project("cloudproj", tmp_path)

        result = runner.invoke(app, ["sync", "--project", "cloudproj"])
        combined = result.output + (result.stderr or "")

        assert result.exit_code == 1, combined
        assert "Warning: this is a cloud-mode project" in combined
        assert "not authenticated" in combined
        assert "Synced cloudproj" not in result.output

    def test_local_project_without_auth_succeeds(self, project_store):
        """Local-only projects sync cleanly without auth — nothing to upload
        is not an error."""
        result = runner.invoke(app, ["sync"])
        combined = result.output + (result.stderr or "")

        assert result.exit_code == 0, combined
        assert "local-only project; nothing to upload" in result.output
        assert "Synced testproj" not in result.output
        assert "Warning: this is a cloud-mode project" not in combined

    def test_cloud_project_with_token_succeeds(self, tmp_path, monkeypatch):
        """With an Auth0 token and the presign helpers mocked to succeed, the
        cloud-mode project syncs and reports success."""
        import json
        from unittest.mock import MagicMock

        import httpx

        from nauro.store.config import save_config

        _scaffolded_cloud_project("cloudwithauth", tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )

        def ok(payload):
            return httpx.Response(
                200,
                content=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"},
            )

        def fake_post(url, **kwargs):
            ops = kwargs.get("json", {}).get("operations", [])
            return ok(
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
                }
            )

        put_response = MagicMock(spec=httpx.Response)
        put_response.status_code = 200
        put_response.headers = {"ETag": '"e_pushed"'}

        with (
            patch(
                "nauro.sync.remote.httpx.get",
                return_value=ok({"files": [], "next_cursor": None}),
            ),
            patch("nauro.sync.remote.httpx.post", side_effect=fake_post),
            patch("nauro.sync.remote.httpx.put", return_value=put_response),
        ):
            result = runner.invoke(app, ["sync", "--project", "cloudwithauth"])

        combined = result.output + (result.stderr or "")
        assert result.exit_code == 0, combined
        assert "Synced cloudwithauth" in result.output
        assert "Warning: this is a cloud-mode project" not in combined


class TestSyncPullSurfacesAndMerges:
    """End-to-end ``nauro sync`` pull behaviour through the shared core.

    A clean pull echoes a "Merged N file(s)" line; a union-merge failure
    surfaces and exits nonzero rather than reporting a partial success.
    """

    @staticmethod
    def _seed_cloud_auth(name: str, tmp_path: Path):
        import json as _json

        from nauro.store.config import save_config

        store = _scaffolded_cloud_project(name, tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        return store, _json

    @staticmethod
    def _http_ok(payload, _json):
        import httpx

        return httpx.Response(
            200,
            content=_json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    def test_clean_pull_echoes_merged_count(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        import httpx

        store, _json = self._seed_cloud_auth("mergedcount", tmp_path)
        rel = "decisions/099-remote.md"

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return self._http_ok(
                    {
                        "files": [{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}],
                        "next_cursor": None,
                    },
                    _json,
                )
            return httpx.Response(200, content=b"# 099\nfresh remote body\n")

        def fake_post(url, **kwargs):
            ops = kwargs.get("json", {}).get("operations", [])
            return self._http_ok(
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
                _json,
            )

        put_response = MagicMock(spec=httpx.Response)
        put_response.status_code = 200
        put_response.headers = {"ETag": '"e_pushed"'}

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", side_effect=fake_post),
            patch("nauro.sync.remote.httpx.put", return_value=put_response),
        ):
            result = runner.invoke(app, ["sync", "--project", "mergedcount"])

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert "Merged 1 file(s) from remote" in result.output

    def test_union_merge_failure_exits_one(self, tmp_path, monkeypatch):
        import httpx

        from nauro.sync.merge import UnionMergeError
        from nauro.sync.state import FileState, SyncState, save_state

        store, _json = self._seed_cloud_auth("mergefail", tmp_path)
        rel = "decisions/051-conflicted.md"
        local_file = store / rel
        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_bytes(b"# 051\nlocal body\n")

        state = SyncState()
        state.files[rel] = FileState(
            local_sha256="old_sha",
            remote_etag='"old_etag"',
            last_sync="2026-05-16T00:00:00Z",
        )
        save_state(store, state)

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return self._http_ok(
                    {
                        "files": [
                            {"path": rel, "etag": '"new_etag"', "size": 1, "last_modified": "x"}
                        ],
                        "next_cursor": None,
                    },
                    _json,
                )
            return httpx.Response(200, content=b"# 051\nremote body\n")

        def fake_post(url, **kwargs):
            ops = kwargs.get("json", {}).get("operations", [])
            return self._http_ok(
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
                _json,
            )

        def boom(*args, **kwargs):
            raise UnionMergeError("simulated git failure")

        monkeypatch.setattr("nauro.sync.pull.resolve_conflict", boom)

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", side_effect=fake_post),
        ):
            result = runner.invoke(app, ["sync", "--project", "mergefail"])

        assert result.exit_code == 1
        assert isinstance(result.exception, UnionMergeError)


class TestLinkCloudRefusesWithoutAuth:
    """`nauro link --cloud` must refuse when the install has no Auth0 token —
    presigned URLs are minted server-side from the bearer, so without one we
    cannot upload, regardless of whether static IAM creds happen to be set.
    """

    def test_link_cloud_refuses_when_not_authenticated(self, tmp_path, monkeypatch):
        """A local-mode repo + no Auth0 token → refusal, no network call."""
        from nauro.store.config import save_config

        # Empty config — no auth section means no access_token.
        save_config({})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NAURO_API_URL", "https://example.test")

        init_result = runner.invoke(app, ["init", "blockedlink"])
        assert init_result.exit_code == 0, init_result.output

        result = runner.invoke(app, ["link", "--cloud"])
        combined = result.output + (result.stderr or "")

        assert result.exit_code == 1, combined
        assert "Cannot link 'blockedlink' to the cloud" in combined
        assert "Run 'nauro auth login'" in combined
