"""Tests for the shared pull core (``nauro.sync.pull``).

``run_pull`` is the single pull-and-merge implementation behind both
``nauro sync`` and the SessionStart hook. The two callers differ only in
their :class:`~nauro.sync.pull.Reporter`: the CLI echoes and re-raises on
a union-merge failure, the hook logs and swallows. These tests drive the
core directly with both reporter flavors plus a recording stub, and pin
the renumber-on-collision helper byte-for-byte against the canonical
cases the hook previously owned.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.config import save_config
from nauro.store.registry import register_project, register_project_v2
from nauro.sync.merge import UnionMergeError
from nauro.sync.pull import _renumber_decision_if_collision, run_pull
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


def _manifest(files, next_cursor=None) -> httpx.Response:
    return _ok(200, {"files": files, "next_cursor": next_cursor})


def _presign(ops) -> httpx.Response:
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


class _RecordingReporter:
    """Records messages; ``on_merge_failure`` returns the configured policy."""

    def __init__(self, *, reraise: bool) -> None:
        self.reraise = reraise
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.merge_failures: list[tuple[str, Exception]] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def on_merge_failure(self, relative_path: str, exc: Exception) -> bool:
        self.merge_failures.append((relative_path, exc))
        return self.reraise


# --- run_pull happy path (both reporter policies behave identically here) ---


class TestRunPullCleanPull:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pullcore", tmp_path)
        _seed_token()
        return store

    @pytest.mark.parametrize("reraise", [True, False])
    def test_clean_pull_writes_file_and_updates_state(self, cloud_store, reraise):
        rel = "decisions/099-remote.md"
        manifest = _manifest([{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}])
        presign = _presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"# 099\nfresh remote body\n")

        reporter = _RecordingReporter(reraise=reraise)
        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, reporter)

        assert merged == 1
        assert (cloud_store / rel).read_bytes() == b"# 099\nfresh remote body\n"
        state = load_state(cloud_store)
        assert state.files[rel].remote_etag == '"new"'
        assert reporter.infos == ["Merged 1 file(s) from remote"]
        assert reporter.warns == []

    @pytest.mark.parametrize("reraise", [True, False])
    def test_append_only_conflict_invokes_resolve_and_writes_merge(self, cloud_store, reraise):
        # state_history.md is append-only with section-aware set-union merge.
        rel = "state_history.md"
        local = cloud_store / rel
        local.write_text("## History\n\nlocal entry\n")
        local_sha = compute_sha256(local)

        state = SyncState()
        state.files[rel] = FileState(
            local_sha256="old_sha",
            remote_etag='"old_etag"',
            last_sync="2026-05-16T00:00:00Z",
        )
        save_state(cloud_store, state)

        manifest = _manifest([{"path": rel, "etag": '"new_etag"', "size": 1, "last_modified": "x"}])
        presign = _presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"## History\n\nremote entry\n")

        reporter = _RecordingReporter(reraise=reraise)
        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, reporter)

        assert merged == 1
        merged_bytes = local.read_bytes()
        # Union of both sides — neither entry was dropped.
        assert b"local entry" in merged_bytes
        assert b"remote entry" in merged_bytes
        assert compute_sha256(local) != local_sha
        assert reporter.merge_failures == []


# --- the bug-fix pin: decision-number collision renumbers, never overwrites ---


class TestRunPullDecisionCollision:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("collisioncore", tmp_path)
        _seed_token()
        return store

    def test_colliding_decision_written_as_next_sequential_file_echo_reporter(self, cloud_store):
        """The CLI (echo) reporter is the reconciled path that previously
        overwrote. A pulled decision whose number collides with a local one is
        written as the NEXT sequential file with its H1 body number rewritten;
        the local file is left untouched."""
        from nauro.cli.commands.sync import _EchoReporter

        decisions_dir = cloud_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        local_file = decisions_dir / "003-local-decision.md"
        local_file.write_bytes(b"# 003 \xe2\x80\x94 Local decision\n\nLocal body\n")
        local_original = local_file.read_bytes()

        rel = "decisions/003-remote-decision.md"
        manifest = _manifest([{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}])
        presign = _presign([{"verb": "GET", "path": rel}])

        remote_body = b"# 003 \xe2\x80\x94 Remote decision\n\nRemote body\n"

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=remote_body)

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, _EchoReporter())

        assert merged == 1
        # Local decision untouched — this is the data-loss bug the fix closes.
        assert local_file.read_bytes() == local_original
        # Remote landed as the next sequential file with the H1 number rewritten.
        new_file = decisions_dir / "004-remote-decision.md"
        assert new_file.exists()
        new_bytes = new_file.read_bytes()
        assert new_bytes == b"# 004 \xe2\x80\x94 Remote decision\n\nRemote body\n"
        # State keys the renumbered path, not the colliding incoming one.
        state = load_state(cloud_store)
        assert "decisions/004-remote-decision.md" in state.files
        assert rel not in state.files


# --- union-merge routing: one test per reporter policy ---


class TestRunPullUnionMergeRouting:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("mergecore", tmp_path)
        _seed_token()
        return store

    def _setup_conflict(self, cloud_store):
        rel = "decisions/050-conflicted.md"
        local_file = cloud_store / rel
        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_bytes(b"# 050\nlocal body\n")

        state = SyncState()
        state.files[rel] = FileState(
            local_sha256="old_sha",
            remote_etag='"old_etag"',
            last_sync="2026-05-16T00:00:00Z",
        )
        save_state(cloud_store, state)

        manifest = _manifest([{"path": rel, "etag": '"new_etag"', "size": 1, "last_modified": "x"}])
        presign = _presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"# 050\nremote body\n")

        return rel, local_file, fake_get, presign

    def test_echo_reporter_reraises_on_union_merge_failure(self, cloud_store, monkeypatch):
        """The echo reporter returns True from on_merge_failure → run_pull
        re-raises so nauro sync fails loud. The local file is left untouched."""
        from nauro.cli.commands.sync import _EchoReporter

        rel, local_file, fake_get, presign = self._setup_conflict(cloud_store)
        original = local_file.read_bytes()

        def boom(*args, **kwargs):
            raise UnionMergeError("simulated git failure")

        monkeypatch.setattr("nauro.sync.merge._union_merge", boom)

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
            pytest.raises(UnionMergeError),
        ):
            run_pull(CLOUD_PID, cloud_store, _EchoReporter())

        assert local_file.read_bytes() == original

    def test_logging_reporter_swallows_union_merge_failure(self, cloud_store, monkeypatch):
        """The logging reporter returns False → run_pull swallows the failure,
        returns without raising, and leaves the file byte-identical."""
        from nauro.sync.hooks import _LoggingReporter

        rel, local_file, fake_get, presign = self._setup_conflict(cloud_store)
        original = local_file.read_bytes()

        def boom(*args, **kwargs):
            raise UnionMergeError("simulated git failure")

        monkeypatch.setattr("nauro.sync.merge._union_merge", boom)

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, _LoggingReporter())

        # The failed file was skipped, not counted, and never raised.
        assert merged == 0
        assert local_file.read_bytes() == original


# --- renumber helper: byte-identical to the canonical pre-port cases ---


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
        # Byte-identical to the old regex rewrite: only the number changed.
        assert out == b"# 004 \xe2\x80\x94 Remote decision\n\nRemote content"

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
        assert out == b"# 007 \xe2\x80\x94 Incoming\n\nContent"

    def test_collision_renumbers_hyphen_separator_heading(self, project_store):
        """The old regex group ``( [—-])`` also matched a plain ASCII hyphen
        separator; the string-ops rewrite must too."""
        decisions_dir = project_store / "decisions"
        decisions_dir.mkdir(exist_ok=True)
        (decisions_dir / "008-local.md").write_text("# 008 - Local")

        content = b"# 008 - Remote decision\n\nRemote content"
        rel, out = _renumber_decision_if_collision(
            project_store,
            "decisions/008-remote.md",
            content,
        )

        assert rel == "decisions/009-remote.md"
        assert out == b"# 009 - Remote decision\n\nRemote content"

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
