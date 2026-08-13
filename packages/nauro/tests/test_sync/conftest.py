"""Shared helpers for the test_sync test suite.

The fake server, the canonical decision bodies, and the store-shaping helpers
live here rather than in whichever test module happened to define them first:
four modules build the same fixtures, and importing them from a sibling test
made the import graph say something about the tests that is not true.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.registry import register_project_v2
from nauro.sync.pull import run_pull
from nauro.sync.state import FileState, compute_sha256, load_state, save_state
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import seed_auth_config

CLOUD_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _scaffolded_cloud_project(name: str, repo_path: Path, project_id: str | None = None) -> Path:
    """Register a cloud-mode v2 project, scaffold its store, and return the store path.

    When ``project_id`` is omitted a ULID is minted automatically. Tests that
    pass a project id to a hook (``pull_before_session`` / ``push_after_write``)
    or presign call must supply ``project_id`` explicitly here so the registry
    lookup that gates those paths (``is_cloud_project``) resolves the same id —
    otherwise the hook early-returns at the project-not-found gate and the test
    silently exercises a shallower path.
    """
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


def _seed_token() -> None:
    seed_auth_config(variant="sync")


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


# A canonical decision carrying the base_commit provenance stamp as its
# trailing frontmatter key. Sync moves raw bytes, so stamped files must
# round-trip byte-identically like any other decision.
_STAMPED_SHA = "a1b2c3d4" * 5
_STAMPED_DECISION = (
    "---\n"
    "date: 2026-08-05\n"
    "version: 1\n"
    "status: active\n"
    "confidence: high\n"
    "decision_type: null\n"
    "reversibility: null\n"
    "source: null\n"
    "files_affected: []\n"
    "supersedes: null\n"
    "superseded_by: null\n"
    f"base_commit: {_STAMPED_SHA}\n"
    "---\n\n"
    "# 099 — Remote stamped decision\n\n"
    "## Decision\n\nChose A.\n"
).encode()


class _RecordingReporter:
    """Records reported messages."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)


def decision_bytes(
    num: int,
    title: str,
    rationale: str = "Chose A.",
    *,
    status: str = "active",
    supersedes: str | None = None,
    superseded_by: str | None = None,
    separator: str = "—",
) -> bytes:
    """A canonical decision file body, parseable by ``parse_decision``."""
    return (
        "---\n"
        "date: 2026-08-10\n"
        "version: 1\n"
        f"status: {status}\n"
        "confidence: high\n"
        "decision_type: null\n"
        "reversibility: null\n"
        "source: null\n"
        "files_affected: []\n"
        f"supersedes: {repr(supersedes) if supersedes else 'null'}\n"
        f"superseded_by: {repr(superseded_by) if superseded_by else 'null'}\n"
        "---\n\n"
        f"# {num:03d} {separator} {title}\n\n"
        "## Decision\n\n"
        f"{rationale}\n"
    ).encode()


def write_local_decision(store, filename: str, content: bytes) -> object:
    decisions = store / "decisions"
    decisions.mkdir(exist_ok=True)
    path = decisions / filename
    path.write_bytes(content)
    return path


def pull_report(store, entries, *, reporter=None, etags=None):
    """Run one pull against a fake server holding exactly ``entries``.

    ``entries`` is an ordered list of ``(path, body)`` pairs so the tests can
    pin manifest order where order is the thing under test. Returns the full
    :class:`~nauro.sync.pull.PullReport`; :func:`pull` is the merged-count form
    most tests want.
    """
    reporter = reporter or _RecordingReporter()
    etags = etags or {}
    bodies = dict(entries)
    manifest = _manifest(
        [
            {
                "path": rel,
                "etag": etags.get(rel, f'"{rel}-v1"'),
                "size": len(body),
                "last_modified": "x",
            }
            for rel, body in entries
        ]
    )
    presign = _presign([{"verb": "GET", "path": rel} for rel, _body in entries])

    def fake_get(url, **kwargs):
        if "/sync/manifest" in url:
            return manifest
        return httpx.Response(200, content=bodies[url.split("/GET/", 1)[1]])

    with (
        patch("nauro.sync.remote.httpx.Client.get", side_effect=fake_get),
        patch("nauro.sync.remote.httpx.Client.post", return_value=presign),
    ):
        report = run_pull(CLOUD_PID, store, reporter)
    return report, reporter


def pull(store, entries, *, reporter=None, etags=None):
    """Run one pull, returning ``(merged count, reporter)``."""
    report, reporter = pull_report(store, entries, reporter=reporter, etags=etags)
    return report.merged, reporter


def entry_names(directory) -> set[str]:
    """Actual on-disk entry names.

    ``Path.exists()`` folds case on macOS and Windows, so an assertion about
    what a pull did or did not write has to read the directory itself.
    """
    return {entry.name for entry in directory.iterdir()} if directory.is_dir() else set()


def track(store, rel: str) -> None:
    """Record a sync-state entry for a local file, as a completed push would."""
    state = load_state(store)
    state.files[rel] = FileState(
        local_sha256=compute_sha256(store / rel),
        remote_etag='"pushed"',
        last_sync="2026-08-10T00:00:00Z",
    )
    save_state(store, state)
