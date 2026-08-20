"""A corrupt snapshot file must not brick log / sync / diff.

Snapshot JSON is written to disk and can be left truncated by an interrupted
write (Ctrl-C, sleep, disk full) or hand-edited. The read paths used to call
json.loads unguarded, so one bad file crashed `nauro log`, `nauro sync` (capture
reads the existing snapshots to compute the next version), and diff. They now
skip an unreadable snapshot with a warning; capture stays atomic.
"""

from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.store.snapshot import (
    capture_snapshot,
    find_snapshot_near_date,
    list_snapshots,
)
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _store_with_two_snapshots(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "p"
    scaffold_project_store("p", store_path)
    capture_snapshot(store_path, trigger="first")
    capture_snapshot(store_path, trigger="second")
    return store_path


def _snap(store_path: Path, version: int) -> Path:
    return store_path / "snapshots" / f"v{version:03d}.json"


def test_list_snapshots_skips_corrupt(tmp_path: Path):
    store_path = _store_with_two_snapshots(tmp_path)
    # Truncated JSON body.
    _snap(store_path, 3).write_text('{"version": 3, "timestamp": "2026-01-01T0')
    # Valid JSON but missing the required version key.
    _snap(store_path, 4).write_text('{"timestamp": "2026-01-01T00:00:00+00:00"}')

    snaps = list_snapshots(store_path)
    versions = {s["version"] for s in snaps}
    assert versions == {1, 2}  # the two corrupt files are skipped, no crash


def test_find_snapshot_near_date_skips_corrupt(tmp_path: Path):
    store_path = _store_with_two_snapshots(tmp_path)
    _snap(store_path, 3).write_text("not json at all")
    from datetime import datetime, timezone

    # Must not raise on the corrupt v003.
    result = find_snapshot_near_date(store_path, datetime.now(timezone.utc))
    assert result is not None and result["version"] in {1, 2}


def test_capture_survives_corrupt_snapshot(tmp_path: Path):
    store_path = _store_with_two_snapshots(tmp_path)
    _snap(store_path, 3).write_text('{"version": 3, "timestamp": "trunc')

    # sync's capture reads existing snapshots to compute the next version; a
    # corrupt v003 must not block it. The corrupt slot is overwritten.
    version = capture_snapshot(store_path, trigger="after-corruption")
    assert version == 3
    assert not list(store_path.glob("snapshots/*.tmp"))  # atomic write left no temp
    # The slot is now valid and the read surface recovers.
    assert {s["version"] for s in list_snapshots(store_path)} == {1, 2, 3}


def test_capture_survives_corrupt_older_snapshot_during_prune(tmp_path: Path):
    store_path = _store_with_two_snapshots(tmp_path)
    # Corrupt an OLDER snapshot, then capture a NEW one. Capture's prune step
    # reads every v*.json, so a corrupt sibling must not crash it.
    _snap(store_path, 1).write_text("{ not valid json")

    version = capture_snapshot(store_path, trigger="newer")
    assert version == 3
    assert not list(store_path.glob("snapshots/*.tmp"))


def test_bad_timestamp_snapshot_skipped_by_date_search_and_prune(tmp_path: Path):
    store_path = _store_with_two_snapshots(tmp_path)
    # Valid JSON with a version but an unparseable timestamp: the timestamp-
    # parsing paths (find_snapshot_near_date, prune) must skip it, not raise.
    _snap(store_path, 3).write_text('{"version": 3, "timestamp": "not-a-date"}')
    from datetime import datetime, timezone

    result = find_snapshot_near_date(store_path, datetime.now(timezone.utc))
    assert result is not None and result["version"] in {1, 2}

    # capture runs prune, which parses every timestamp — must not raise.
    version = capture_snapshot(store_path, trigger="after-bad-ts")
    assert version == 4
    assert not list(store_path.glob("snapshots/*.tmp"))
    # An undated snapshot is listed and versioned but never pruned.
    assert _snap(store_path, 3).exists()
    assert 3 in {s["version"] for s in list_snapshots(store_path)}


def test_naive_timestamp_snapshot_treated_as_undated(tmp_path: Path):
    store_path = _store_with_two_snapshots(tmp_path)
    # fromisoformat accepts a timezone-naive value, but a naive datetime cannot
    # compare against the UTC-aware prune cutoffs — it must count as undated.
    _snap(store_path, 3).write_text('{"version": 3, "timestamp": "2026-01-01T00:00:00"}')
    from datetime import datetime, timezone

    result = find_snapshot_near_date(store_path, datetime.now(timezone.utc))
    assert result is not None and result["version"] in {1, 2}

    version = capture_snapshot(store_path, trigger="after-naive-ts")
    assert version == 4
    assert _snap(store_path, 3).exists()
    assert 3 in {s["version"] for s in list_snapshots(store_path)}


def test_malformed_token_count_never_crashes(tmp_path: Path):
    store_path = _store_with_two_snapshots(tmp_path)
    base = '"timestamp": "2026-01-01T00:00:00+00:00", "files": {}'
    _snap(store_path, 3).write_text(f'{{"version": 3, "token_count": null, {base}}}')
    _snap(store_path, 4).write_text(f'{{"version": 4, "token_count": {{"x": 1}}, {base}}}')

    by_version = {s["version"]: s for s in list_snapshots(store_path)}
    assert by_version[3]["token_count"] == 0  # null coerces to 0
    assert 4 not in by_version  # unconvertible value: skipped, no crash


def test_nauro_log_does_not_crash_on_corrupt_snapshot(tmp_path: Path, monkeypatch):
    _pid, store_path = register_project_v2("p", [tmp_path])
    scaffold_project_store("p", store_path)
    capture_snapshot(store_path, trigger="ok")
    _snap(store_path, 2).write_text("{ broken")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
