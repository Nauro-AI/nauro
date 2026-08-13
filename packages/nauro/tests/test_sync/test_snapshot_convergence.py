"""Regression coverage for legacy snapshot convergence."""

from __future__ import annotations

import json

from nauro_core.snapshot import serialize_snapshot

from nauro.store.snapshot import capture_snapshot
from nauro.sync.pull import PullReport
from tests.test_sync.conftest import (
    _scaffolded_cloud_project,
    _seed_token,
    pull_report,
)


def _snapshot(version: int, timestamp: str) -> bytes:
    return (
        json.dumps(
            serialize_snapshot(
                version=version,
                timestamp=timestamp,
                trigger="test",
                files={},
            )
        )
        + "\n"
    ).encode()


def test_pull_capture_prune_pull_converges(tmp_path):
    store = _scaffolded_cloud_project("snapshot-convergence", tmp_path)
    _seed_token()
    snapshots = store / "snapshots"
    local_v1 = _snapshot(1, "2025-01-01T00:00:00+00:00")
    local_v2 = _snapshot(2, "2025-01-20T00:00:00+00:00")
    (snapshots / "v001.json").write_bytes(local_v1)
    (snapshots / "v002.json").write_bytes(local_v2)
    remote = [
        ("snapshots/v001.json", b'{"remote": 1}\n'),
        ("snapshots/v002.json", b'{"remote": 2}\n'),
    ]

    first, _reporter = pull_report(store, remote)

    assert first == PullReport()
    assert not (store / ".conflict-backup").exists()
    assert (snapshots / "v001.json").read_bytes() == local_v1
    assert (snapshots / "v002.json").read_bytes() == local_v2

    assert capture_snapshot(store, trigger="test") == 3
    assert not (snapshots / "v001.json").exists()
    assert (snapshots / "v002.json").exists()
    assert (snapshots / "v003.json").exists()

    second, _reporter = pull_report(store, remote)

    assert second == PullReport()
    assert not (snapshots / "v001.json").exists()
    assert (snapshots / "v002.json").read_bytes() == local_v2
    assert (snapshots / "v003.json").exists()
