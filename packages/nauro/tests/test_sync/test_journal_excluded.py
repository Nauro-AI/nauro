"""The write-path provenance journal is excluded from cloud sync (push + pull).

The unit rule lives in ``test_merge.py::TestShouldSkip``; these drive the real
push enumeration and pull manifest walk to prove the journal never leaves — nor
lands on — a machine over sync.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from nauro.sync.pull import run_pull
from nauro.sync.push import push_changed_files
from tests.conftest import seed_auth_config
from tests.test_sync.conftest import CLOUD_PID, _scaffolded_cloud_project


def _ok(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


class _RecordingReporter:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)


def _seed_journal(store: Path) -> None:
    journal = store / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    (journal / "events.jsonl").write_text('{"operation": "update_state"}\n', encoding="utf-8")
    (journal / ".lock").write_text("", encoding="utf-8")


def test_push_does_not_enumerate_journal(tmp_path):
    store = _scaffolded_cloud_project("pushj", tmp_path, project_id=CLOUD_PID)
    seed_auth_config(variant="sync")
    _seed_journal(store)

    captured: dict[str, list] = {}

    def fake_presign(project_id, operations):
        captured["ops"] = operations
        return []  # no URLs → nothing is uploaded

    with patch("nauro.sync.remote.request_presigned_urls", side_effect=fake_presign):
        push_changed_files(CLOUD_PID, store)

    paths = [op["path"] for op in captured["ops"]]
    # A normal store file is enumerated; nothing under journal/ ever is.
    assert "project.md" in paths
    assert not any(p.startswith("journal/") for p in paths)


def test_pull_skips_remote_journal_path(tmp_path):
    store = _scaffolded_cloud_project("pullj", tmp_path, project_id=CLOUD_PID)
    seed_auth_config(variant="sync")

    rel = "journal/events.jsonl"
    manifest = _ok(200, {"files": [{"path": rel, "etag": '"new"', "size": 1}], "next_cursor": None})

    def fake_get(url, **kwargs):
        if "/sync/manifest" in url:
            return manifest
        raise AssertionError("journal path must never be fetched")

    reporter = _RecordingReporter()
    with patch("nauro.sync.remote.httpx.get", side_effect=fake_get):
        merged = run_pull(CLOUD_PID, store, reporter)

    assert merged == 0
    assert not (store / rel).exists()
    assert reporter.warns == []
