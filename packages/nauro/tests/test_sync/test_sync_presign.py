"""Tests for the manifest + presign sync transport.

Three concerns, three sections:

* Mode detection — cloud-mode + token routes through presign; cloud-mode
  without a token warns and fails; non-cloud projects no-op.
* Pull flow (manifest pagination, diff against state, presign GETs, conflict).
* Push flow (SHA diff, presign PUTs, 401 refresh).

All HTTP is mocked at the module's ``httpx`` import, matching the existing
``test_link_cloud`` and ``test_auth`` patterns — moto would not catch this
layer (the CLI never talks to S3 directly).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.sync.state import (
    FileState,
    SyncState,
    compute_sha256,
    load_state,
    save_state,
)
from nauro.templates.scaffolds import scaffold_project_store

CLOUD_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


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


def _ok(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _seed_token(access_token: str = "tok_orig", refresh_token: str = "refresh_orig") -> None:
    save_config(
        {
            "auth": {
                "sub": "auth0|test",
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
        }
    )


# --- mode detection ---


class TestModeDetection:
    """Routing predicates for ``_pull_from_cloud`` / ``_push_to_cloud``:

    * cloud-mode + Auth0 token       → presign
    * cloud-mode without a token     → warn + return False (push), 0 (pull)
    * non-cloud (v1 or v2-local)     → no-op
    """

    def test_cloud_mode_with_token_hits_manifest(self, tmp_path, monkeypatch):
        store = _scaffolded_cloud_project("authproj", tmp_path)
        _seed_token()

        with (
            patch(
                "nauro.sync.remote.httpx.get",
                return_value=_ok(200, {"files": [], "next_cursor": None}),
            ) as mock_get,
            patch("nauro.sync.remote.httpx.post") as mock_post,
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            _pull_from_cloud(store.name, store)

        called_urls = [c.args[0] for c in mock_get.call_args_list]
        assert any("/sync/manifest" in url for url in called_urls)
        mock_post.assert_not_called()

    def test_cloud_mode_without_token_push_warns_and_fails(self, tmp_path, monkeypatch):
        store = _scaffolded_cloud_project("strandedproj", tmp_path)
        save_config({})

        from nauro.cli.commands.sync import _push_to_cloud

        ok = _push_to_cloud(store.name, store)
        assert ok is False

    def test_stale_legacy_config_keys_load_cleanly(self, tmp_path, monkeypatch):
        """A user who upgraded from the legacy direct-S3 path still has
        ``sync.access_key_id`` and friends in ``~/.nauro/config.json``. The
        config loader must tolerate them as inert data and routing must go
        through presign anyway."""
        from nauro.store.config import load_config

        store = _scaffolded_cloud_project("upgradedproj", tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                },
                "sync": {
                    "bucket_name": "b",
                    "region": "us-east-1",
                    "access_key_id": "stale-akid",
                    "secret_access_key": "stale-secret",
                },
            }
        )

        # Loader returns the dict intact; stale keys are present but inert.
        data = load_config()
        assert data["sync"]["access_key_id"] == "stale-akid"

        with (
            patch(
                "nauro.sync.remote.httpx.get",
                return_value=_ok(200, {"files": [], "next_cursor": None}),
            ) as mock_get,
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            _pull_from_cloud(store.name, store)

        called_urls = [c.args[0] for c in mock_get.call_args_list]
        assert any("/sync/manifest" in url for url in called_urls)


# --- pull flow ---


class TestPullViaPresign:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pullproj", tmp_path, project_id=CLOUD_PID)
        _seed_token()
        return store

    def _manifest_response(
        self, files: list[dict], next_cursor: str | None = None
    ) -> httpx.Response:
        return _ok(200, {"files": files, "next_cursor": next_cursor})

    def _presign_response(self, ops: list[dict]) -> httpx.Response:
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

    def test_manifest_pagination_two_pages_collapse(self, cloud_store):
        """The CLI must walk ``next_cursor`` until the server returns None."""
        page_one = self._manifest_response(
            [{"path": "decisions/001.md", "etag": '"e1"', "size": 1, "last_modified": "x"}],
            next_cursor="cursor_abc",
        )
        page_two = self._manifest_response(
            [{"path": "decisions/002.md", "etag": '"e2"', "size": 1, "last_modified": "x"}],
            next_cursor=None,
        )

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                params = kwargs.get("params") or {}
                if params.get("cursor") == "cursor_abc":
                    return page_two
                return page_one
            return httpx.Response(200, content=b"file body")

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "get", side_effect=fake_get),
            patch.object(
                remote.httpx,
                "post",
                return_value=self._presign_response(
                    [
                        {"verb": "GET", "path": "decisions/001.md"},
                        {"verb": "GET", "path": "decisions/002.md"},
                    ]
                ),
            ),
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            merged = _pull_from_cloud(cloud_store.name, cloud_store)

        # Both files came back through the diff.
        assert merged == 2

    def test_manifest_match_no_presign_call(self, cloud_store):
        """Manifest entries that already match local state → no presign call."""
        rel = "decisions/037-test.md"
        target = cloud_store / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 037\nremote content\n")

        state = SyncState()
        state.files[rel] = FileState(
            local_sha256=compute_sha256(target),
            remote_etag='"e1"',
            last_sync="2026-05-16T00:00:00Z",
        )
        save_state(cloud_store, state)

        manifest = self._manifest_response(
            [{"path": rel, "etag": '"e1"', "size": 1, "last_modified": "x"}],
            next_cursor=None,
        )

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "get", return_value=manifest),
            patch.object(remote.httpx, "post") as mock_post,
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            merged = _pull_from_cloud(cloud_store.name, cloud_store)

        assert merged == 0
        mock_post.assert_not_called()

    def test_etag_mismatch_triggers_presign_get_and_writes_file(self, cloud_store):
        rel = "decisions/099-remote.md"
        manifest = self._manifest_response(
            [{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}],
            next_cursor=None,
        )
        presign = self._presign_response([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"# 099\nfresh remote body\n")

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "get", side_effect=fake_get),
            patch.object(remote.httpx, "post", return_value=presign) as mock_post,
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            merged = _pull_from_cloud(cloud_store.name, cloud_store)

        # Presign request: payload carries project_id and the GET op.
        assert mock_post.call_count == 1
        sent = mock_post.call_args.kwargs["json"]
        assert sent["project_id"] == CLOUD_PID
        assert sent["operations"] == [{"verb": "GET", "path": rel}]

        # Server payload landed in the local file and state updated.
        pulled = cloud_store / rel
        assert pulled.read_bytes() == b"# 099\nfresh remote body\n"
        state = load_state(cloud_store)
        assert state.files[rel].remote_etag == '"new"'
        assert merged == 1

    def test_conflict_path_runs_resolve_conflict(self, cloud_store):
        """When local + remote both moved, resolve_conflict is called and
        the result is written. last-write-wins keeps the local body."""
        rel = "stack.md"  # non-append-only → last-write-wins
        local = cloud_store / rel
        # local already exists from scaffold; rewrite it to a known local body
        local.write_text("local sprint plan\n")
        local_sha = compute_sha256(local)

        state = SyncState()
        state.files[rel] = FileState(
            local_sha256="old_sha",
            remote_etag='"old_etag"',
            last_sync="2026-05-16T00:00:00Z",
        )
        save_state(cloud_store, state)

        manifest = self._manifest_response(
            [{"path": rel, "etag": '"new_etag"', "size": 1, "last_modified": "x"}],
            next_cursor=None,
        )
        presign = self._presign_response([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"remote sprint plan\n")

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "get", side_effect=fake_get),
            patch.object(remote.httpx, "post", return_value=presign),
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            merged = _pull_from_cloud(cloud_store.name, cloud_store)

        assert merged == 1
        # last-write-wins kept the local body — verify by sha
        assert compute_sha256(local) == local_sha
        # Backup of the remote losing version should exist.
        backups = list((cloud_store / ".conflict-backup").iterdir())
        assert any("stack.md" in b.name for b in backups)

    def test_manifest_401_refresh_then_retry_succeeds(self, cloud_store):
        manifest_ok = self._manifest_response(
            [{"path": "decisions/001.md", "etag": '"e1"', "size": 1, "last_modified": "x"}],
            next_cursor=None,
        )
        unauthorized = httpx.Response(401, content=b'{"error":"unauthorized"}')
        presign_response = self._presign_response([{"verb": "GET", "path": "decisions/001.md"}])
        refresh_response = _ok(200, {"access_token": "tok_new"})

        manifest_responses = iter([unauthorized, manifest_ok])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return next(manifest_responses)
            return httpx.Response(200, content=b"body")

        def fake_post(url, **kwargs):
            if "/oauth/token" in url:
                return refresh_response
            return presign_response

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "get", side_effect=fake_get) as mock_get,
            patch.object(remote.httpx, "post", side_effect=fake_post) as mock_post,
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            merged = _pull_from_cloud(cloud_store.name, cloud_store)

        assert merged == 1
        # The 401 forced a refresh and a retry of the manifest call.
        manifest_calls = [c for c in mock_get.call_args_list if "/sync/manifest" in c.args[0]]
        refresh_calls = [c for c in mock_post.call_args_list if "/oauth/token" in c.args[0]]
        assert len(manifest_calls) == 2
        assert len(refresh_calls) == 1

    def test_presign_401_refresh_then_retry_succeeds(self, cloud_store):
        manifest = self._manifest_response(
            [{"path": "decisions/001.md", "etag": '"e1"', "size": 1, "last_modified": "x"}],
            next_cursor=None,
        )
        unauthorized = httpx.Response(401, content=b'{"error":"unauthorized"}')
        presign_ok = self._presign_response([{"verb": "GET", "path": "decisions/001.md"}])
        refresh_response = _ok(200, {"access_token": "tok_new"})

        post_responses = iter([unauthorized, presign_ok])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"body")

        def fake_post(url, **kwargs):
            if "/oauth/token" in url:
                return refresh_response
            return next(post_responses)

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "get", side_effect=fake_get),
            patch.object(remote.httpx, "post", side_effect=fake_post) as mock_post,
        ):
            from nauro.cli.commands.sync import _pull_from_cloud

            merged = _pull_from_cloud(cloud_store.name, cloud_store)

        assert merged == 1
        presign_calls = [c for c in mock_post.call_args_list if "/sync/presign" in c.args[0]]
        refresh_calls = [c for c in mock_post.call_args_list if "/oauth/token" in c.args[0]]
        assert len(presign_calls) == 2
        assert len(refresh_calls) == 1


# --- push flow ---


class TestPushViaPresign:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pushproj", tmp_path, project_id=CLOUD_PID)
        _seed_token()
        return store

    def _seed_synced_state(self, store: Path) -> None:
        """Record every existing file as already in sync — push diff is empty."""
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

    def _presign_response(self, ops: list[dict]) -> httpx.Response:
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

    def test_modified_file_minted_via_presign_and_uploaded(self, cloud_store):
        self._seed_synced_state(cloud_store)
        # Touch one file to look modified vs state.
        modified = cloud_store / "stack.md"
        modified.write_text("entirely new stack picks\n")
        new_sha = compute_sha256(modified)

        put_response = MagicMock(spec=httpx.Response)
        put_response.status_code = 200
        put_response.headers = {"ETag": '"e_pushed"'}

        def fake_post(url, **kwargs):
            return self._presign_response(kwargs["json"]["operations"])

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "post", side_effect=fake_post) as mock_post,
            patch.object(remote.httpx, "put", return_value=put_response) as mock_put,
        ):
            from nauro.cli.commands.sync import _push_to_cloud

            ok = _push_to_cloud(cloud_store.name, cloud_store)

        assert ok is True
        # Presign payload carries project_id + a single PUT op for the modified path.
        body = mock_post.call_args.kwargs["json"]
        assert body["project_id"] == CLOUD_PID
        assert body["operations"] == [{"verb": "PUT", "path": "stack.md"}]

        # The PUT used the file's bytes.
        assert mock_put.call_count == 1
        assert mock_put.call_args.kwargs["content"] == b"entirely new stack picks\n"

        # State recorded the new sha + the server-returned etag.
        state = load_state(cloud_store)
        assert state.files["stack.md"].local_sha256 == new_sha
        assert state.files["stack.md"].remote_etag == '"e_pushed"'

    def test_no_local_changes_no_presign_call(self, cloud_store):
        self._seed_synced_state(cloud_store)

        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "post") as mock_post,
            patch.object(remote.httpx, "put") as mock_put,
        ):
            from nauro.cli.commands.sync import _push_to_cloud

            ok = _push_to_cloud(cloud_store.name, cloud_store)

        assert ok is True
        mock_post.assert_not_called()
        mock_put.assert_not_called()

    def test_presign_401_triggers_refresh(self, cloud_store):
        self._seed_synced_state(cloud_store)
        (cloud_store / "stack.md").write_text("modified\n")

        unauthorized = httpx.Response(401, content=b'{"error":"unauthorized"}')
        presign_ok = self._presign_response([{"verb": "PUT", "path": "stack.md"}])
        refresh_ok = _ok(200, {"access_token": "tok_new"})

        post_responses = iter([unauthorized, presign_ok])

        def fake_post(url, **kwargs):
            if "/oauth/token" in url:
                return refresh_ok
            return next(post_responses)

        put_response = MagicMock(spec=httpx.Response)
        put_response.status_code = 200
        put_response.headers = {"ETag": '"e_pushed"'}

        # remote.httpx and auth.httpx are the same module — patching either
        # routes both module's calls through the same mock.
        from nauro.sync import remote

        with (
            patch.object(remote.httpx, "post", side_effect=fake_post) as mock_post,
            patch.object(remote.httpx, "put", return_value=put_response),
        ):
            from nauro.cli.commands.sync import _push_to_cloud

            ok = _push_to_cloud(cloud_store.name, cloud_store)

        assert ok is True
        # Initial 401 + retry against /sync/presign, plus one /oauth/token refresh.
        presign_calls = [c for c in mock_post.call_args_list if "/sync/presign" in c.args[0]]
        refresh_calls = [c for c in mock_post.call_args_list if "/oauth/token" in c.args[0]]
        assert len(presign_calls) == 2
        assert len(refresh_calls) == 1
