"""Tests for nauro sync bidirectional pull-then-push behavior."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.sync.pull import PullReport
from nauro.sync.push import PushReport
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import seed_auth_config
from tests.test_sync.conftest import _scaffolded_cloud_project

runner = CliRunner()


@pytest.fixture()
def project_store(tmp_path: Path, monkeypatch):
    """Set up a project store for testing."""
    _pid, store = register_project_v2("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(tmp_path)
    return store


class TestSyncPullBeforePush:
    """Verify that sync pulls from S3 before pushing."""

    def test_sync_with_s3_calls_pull_before_push(self, project_store, monkeypatch):
        """When S3 is configured, sync should pull then push."""
        call_order = []
        sessions = []

        # We need to patch at the module level where they're defined
        from nauro.cli.commands import sync as sync_mod

        def mock_pull(project_name, store_path, **kwargs):
            call_order.append("pull")
            sessions.append(kwargs["session"])
            return PullReport()

        def mock_push(project_name, store_path, **kwargs):
            call_order.append("push")
            sessions.append(kwargs["session"])
            return PushReport()

        monkeypatch.setattr(sync_mod, "_pull_from_cloud", mock_pull)
        monkeypatch.setattr(sync_mod, "_push_to_cloud", mock_push)

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert call_order == ["pull", "push"]
        assert sessions[0] is sessions[1]

    def test_sync_without_s3_unchanged(self, project_store, monkeypatch):
        """When S3 is not configured, sync should still work (pull is a no-op)."""
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "local-only project; nothing to upload" in result.output
        # No "Pulling from remote" because sync is not configured
        assert "Pulling from remote" not in result.output


class TestSyncExitCode:
    """The exit code says whether the store now holds what the server has.

    A pull that could not write a file used to exit 0, so a script that syncs
    and then reads the store had no way to know it was reading a store the run
    itself knew was short.
    """

    @staticmethod
    def _stub_pull(monkeypatch, report: PullReport) -> None:
        from nauro.cli.commands import sync as sync_mod

        monkeypatch.setattr(sync_mod, "_pull_from_cloud", lambda *_args, **_kwargs: report)

    def test_a_clean_pull_exits_zero(self, project_store, monkeypatch):
        self._stub_pull(monkeypatch, PullReport(merged=3))

        result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0, result.output

    def test_a_refused_file_exits_two(self, project_store, monkeypatch):
        self._stub_pull(monkeypatch, PullReport(merged=1, refused=2))

        result = runner.invoke(app, ["sync"])

        assert result.exit_code == 2, result.output
        assert "2 remote file(s) were not written" in result.output

    def test_a_pull_that_never_read_the_server_exits_two(self, project_store, monkeypatch):
        """An empty report used to say the same thing as a store in step."""
        self._stub_pull(monkeypatch, PullReport(manifest_read=False))

        result = runner.invoke(app, ["sync"])

        assert result.exit_code == 2, result.output
        assert "could not read the server's file list" in result.output

    def test_a_permanent_pull_origin_abort_exits_one_without_success(
        self, project_store, monkeypatch
    ):
        from nauro.cli.commands import sync as sync_mod

        self._stub_pull(
            monkeypatch,
            PullReport(
                manifest_read=False,
                origin_aborted=("https://api.example:443",),
            ),
        )
        monkeypatch.setattr(sync_mod, "is_cloud_project", lambda _project: True)
        monkeypatch.setattr(sync_mod, "_push_to_cloud", lambda *_args, **_kwargs: PushReport())

        result = runner.invoke(app, ["sync"])

        assert result.exit_code == 1, result.output
        assert "permanent remote origin failure" in result.output
        assert "Synced testproj" not in result.output

    def test_the_push_still_runs_before_that_exit(self, project_store, monkeypatch):
        """The push order is unchanged: the snapshot is local work worth keeping."""
        from nauro.cli.commands import sync as sync_mod

        call_order = []

        def mock_pull(_project_name, _store_path, **_kwargs):
            call_order.append("pull")
            return PullReport(manifest_read=False)

        def mock_push(_project_name, _store_path, **_kwargs):
            call_order.append("push")
            return PushReport()

        monkeypatch.setattr(sync_mod, "_pull_from_cloud", mock_pull)
        monkeypatch.setattr(sync_mod, "_push_to_cloud", mock_push)

        result = runner.invoke(app, ["sync"])

        assert call_order == ["pull", "push"]
        assert result.exit_code == 2, result.output

    def test_a_permanent_skip_alone_exits_zero(self, project_store, monkeypatch):
        """A quarantined collision has its own surface and no retry to offer."""
        self._stub_pull(monkeypatch, PullReport(skipped_permanent=1))

        result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0, result.output

    def test_a_push_failure_still_exits_one(self, project_store, monkeypatch):
        from nauro.cli.commands import sync as sync_mod

        self._stub_pull(monkeypatch, PullReport(refused=1))
        monkeypatch.setattr(
            sync_mod,
            "_push_to_cloud",
            lambda *_args, **_kwargs: PushReport(failed=("transport",)),
        )

        result = runner.invoke(app, ["sync"])

        # The command's own failure outranks the pull's unfinished business.
        assert result.exit_code == 1, result.output
        assert "cloud push failed" in result.output
        assert "Synced testproj" not in result.output


class TestSyncSnapshotIsPrimaryWork:
    """The snapshot is sync's own work, so a failure there is a real failure.

    The post-commit seam makes an ancillary snapshot or regen fail open on the
    write commands; sync sits outside that boundary and must stay hard-fail.
    """

    def test_snapshot_failure_exits_nonzero(self, project_store, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("nauro.cli.commands.sync.capture_snapshot", _boom)

        result = runner.invoke(app, ["sync"])

        assert result.exit_code == 1
        assert "local-only project; nothing to upload" not in result.output


class TestSyncPullNoConfig:
    """Verify pull is a no-op when S3 is not configured."""

    def test_pull_returns_an_empty_report_when_not_configured(self, project_store):
        """_pull_from_cloud reports nothing done when sync is not configured."""
        from nauro.cli.commands.sync import _pull_from_cloud

        assert _pull_from_cloud("testproj", project_store) == PullReport()


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
        # The final line leads with the failure, not the local capture.
        assert "Error: cloud push failed for cloudproj" in combined
        assert "will be pushed on the next successful sync" in combined
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
                "nauro.sync.remote.httpx.Client.get",
                return_value=ok({"files": [], "next_cursor": None}),
            ),
            patch("nauro.sync.remote.httpx.Client.post", side_effect=fake_post),
            patch("nauro.sync.remote.httpx.Client.put", return_value=put_response),
        ):
            result = runner.invoke(app, ["sync", "--project", "cloudwithauth"])

        combined = result.output + (result.stderr or "")
        assert result.exit_code == 0, combined
        assert "Synced cloudwithauth" in result.output
        assert "Warning: this is a cloud-mode project" not in combined

    @pytest.mark.parametrize(
        ("failure", "expected_pull_posts"),
        [
            ("manifest-malformed-json", 0),
            ("presign-invalid-shape", 1),
        ],
    )
    def test_invalid_pull_api_response_blocks_same_origin_push_and_success(
        self,
        failure,
        expected_pull_posts,
        tmp_path,
        monkeypatch,
    ):
        import httpx

        _scaffolded_cloud_project("invalidpull", tmp_path)
        seed_auth_config(variant="sync")
        monkeypatch.setenv("NAURO_API_URL", "https://api.test")
        manifest = httpx.Response(
            200,
            json={
                "files": [{"path": "project.md", "etag": '"remote"'}],
                "next_cursor": None,
            },
        )
        invalid_manifest = httpx.Response(200, content=b"{not json")
        invalid_presign = httpx.Response(200, json={"urls": {}})

        def fake_get(url, **_kwargs):
            assert "/sync/manifest" in url
            return invalid_manifest if failure == "manifest-malformed-json" else manifest

        post_calls = 0

        def fake_post(_url, **kwargs):
            nonlocal post_calls
            post_calls += 1
            if failure == "presign-invalid-shape" and post_calls == 1:
                return invalid_presign
            operations = kwargs["json"]["operations"]
            return httpx.Response(
                200,
                json={
                    "urls": [
                        {
                            "verb": operation["verb"],
                            "path": operation["path"],
                            "url": f"https://objects.test/{operation['path']}",
                        }
                        for operation in operations
                    ]
                },
            )

        put_response = httpx.Response(200, headers={"ETag": '"uploaded"'})
        with (
            patch("nauro.sync.remote.httpx.Client.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.Client.post", side_effect=fake_post) as mock_post,
            patch("nauro.sync.remote.httpx.Client.put", return_value=put_response) as mock_put,
        ):
            result = runner.invoke(app, ["sync", "--project", "invalidpull"])

        assert result.exit_code == 1, result.output
        assert "permanent remote origin failure" in result.output
        assert "Synced invalidpull" not in result.output
        assert mock_post.call_count == expected_pull_posts
        mock_put.assert_not_called()


class TestSyncPullSurfacesAndMerges:
    """End-to-end ``nauro sync`` pull behaviour through the shared core.

    A clean pull echoes a "Merged N file(s)" line.
    """

    @staticmethod
    def _seed_cloud_auth(name: str, tmp_path: Path):
        import json as _json

        store = _scaffolded_cloud_project(name, tmp_path)
        seed_auth_config(variant="sync")
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
            patch("nauro.sync.remote.httpx.Client.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.Client.post", side_effect=fake_post),
            patch("nauro.sync.remote.httpx.Client.put", return_value=put_response),
        ):
            result = runner.invoke(app, ["sync", "--project", "mergedcount"])

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert "Merged 1 file(s) from remote" in result.output


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
