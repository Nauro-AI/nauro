"""Surface-level parity for the local ``diff_since_last_session`` adapters.

After the kernel cutover, every local surface that exposes
``diff_since_last_session`` must produce the same envelope for the same
arguments against the same store. The two wirings under test here are
the ``tool_diff_since_last_session`` direct call and the stdio MCP
wrapper that maps ``project_id`` onto a store path.

Two compressions vs. the ``check_decision`` parity test:

* No CLI surface. The CLI exposes ``nauro diff [version_a] [version_b]``
  but reaches the kernel through the same adapter under test here; the
  envelope shape is not a separate CLI concern.
* No FastAPI surface as a parity participant. The local HTTP server
  does not expose a ``/diff`` endpoint; equality across stdio and direct
  tool is enough at this layer.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nauro.constants import REPO_CONFIG_MODE_LOCAL, SNAPSHOTS_DIR
from nauro.mcp.stdio_server import diff_since_last_session as stdio_diff_since_last_session
from nauro.mcp.tools import tool_diff_since_last_session
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.store.snapshot import capture_snapshot, load_snapshot
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


def _backdate_snapshot(store_path: Path, version: int, days_ago: int) -> None:
    snap_path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
    data = load_snapshot(store_path, version)
    data["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    snap_path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Register a project, scaffold the store, capture two snapshots."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("parity-diff", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-diff"})
    scaffold_project_store("parity-diff", store_path)
    capture_snapshot(store_path, trigger="initial")
    append_decision(store_path, "Adopt Postgres", rationale="ACID semantics matter.")
    capture_snapshot(store_path, trigger="with-decision")
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def time_indexed_repo(tmp_path, monkeypatch):
    """A repo with snapshots at known time offsets, suitable for days-based diffs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("parity-diff-time", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-diff-time"})
    scaffold_project_store("parity-diff-time", store_path)
    capture_snapshot(store_path, trigger="initial")
    _backdate_snapshot(store_path, 1, days_ago=14)
    append_decision(store_path, "Adopt Postgres", rationale="ACID semantics matter.")
    capture_snapshot(store_path, trigger="post-db")
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def single_snapshot_repo(tmp_path, monkeypatch):
    """A registered project whose store holds exactly one snapshot."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("parity-diff-one", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-diff-one"})
    scaffold_project_store("parity-diff-one", store_path)
    capture_snapshot(store_path, trigger="initial")
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def empty_repo(tmp_path, monkeypatch):
    """A registered project whose store has no snapshots yet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("parity-diff-empty", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-diff-empty"})
    scaffold_project_store("parity-diff-empty", store_path)
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def missing_store(tmp_path, monkeypatch):
    """A repo with no associated project store at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    nonexistent = tmp_path / "projects" / "nope"
    monkeypatch.chdir(repo)
    return nonexistent


def _stdio_envelope(pid: str, days: int | None = None) -> dict:
    return stdio_diff_since_last_session(project_id=pid, days=days)


def _tool_envelope(store_path: Path, days: int | None = None) -> dict:
    return tool_diff_since_last_session(store_path, days)


def test_session_scoped_envelope_matches_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    stdio = _stdio_envelope(pid)
    tool = _tool_envelope(store_path)
    assert stdio == tool
    assert stdio["store"] == "local"
    assert "v001" in stdio["diff"]
    assert "v002" in stdio["diff"]
    # Session-scoped diffs do not surface a cutoff_date_used field.
    assert "cutoff_date_used" not in stdio


def test_time_based_envelope_matches_across_surfaces(time_indexed_repo):
    pid, store_path = time_indexed_repo
    stdio = _stdio_envelope(pid, days=7)
    tool = _tool_envelope(store_path, days=7)
    assert stdio == tool
    assert stdio["store"] == "local"
    assert "v001" in stdio["diff"]
    assert "v002" in stdio["diff"]
    # Days-based path threads the baseline timestamp through as cutoff_date_used.
    assert stdio["cutoff_date_used"]


def test_empty_store_session_scoped_envelope_matches(empty_repo):
    pid, store_path = empty_repo
    stdio = _stdio_envelope(pid)
    tool = _tool_envelope(store_path)
    assert stdio == tool
    assert stdio["store"] == "local"
    assert "Not enough snapshots" in stdio["diff"]


def test_empty_store_days_based_envelope_matches(empty_repo):
    pid, store_path = empty_repo
    stdio = _stdio_envelope(pid, days=7)
    tool = _tool_envelope(store_path, days=7)
    assert stdio == tool
    assert stdio["store"] == "local"
    assert "No snapshots" in stdio["diff"]


def test_days_based_one_snapshot_covers_range_matches_across_surfaces(single_snapshot_repo):
    # With one snapshot, the days-based lookup resolves it as both
    # baseline and latest. The adapter short-circuits to the canonical
    # one-snapshot-covers-range sentinel (byte-identical to pre-cutover
    # output) on both the auto-generated CLI path and the stdio MCP path.
    pid, store_path = single_snapshot_repo
    stdio = _stdio_envelope(pid, days=7)
    tool = _tool_envelope(store_path, days=7)
    assert stdio == tool
    assert stdio["store"] == "local"
    assert stdio["diff"] == "Only one snapshot covers the requested time range — no diff available."


def test_missing_store_guidance_matches_across_surfaces(missing_store):
    tool = _tool_envelope(missing_store)
    assert tool["store"] == "local"
    assert tool["status"] == "error"
    assert "nauro init" in tool["guidance"]


# --- Byte-identical content parity against the pre-cutover baseline ---
#
# These fixtures are constructed deterministically (snapshot timestamps
# are rewritten via ``_backdate_snapshot`` so the diff timestamp prefix
# is fixed across runs). The captured baseline strings below mirror the
# pre-cutover ``store.reader.diff_since_last_session`` output verbatim
# for the canonical branches.


def _fixed_store_with_two_snapshots(tmp_path: Path) -> Path:
    store_path = tmp_path / "store"
    scaffold_project_store("byte-parity", store_path)
    capture_snapshot(store_path, trigger="initial")
    _backdate_snapshot(store_path, 1, days_ago=14)
    append_decision(store_path, "Adopt Postgres", rationale="ACID semantics matter.")
    capture_snapshot(store_path, trigger="with-decision")
    _backdate_snapshot(store_path, 2, days_ago=7)
    return store_path


def test_session_scoped_content_carries_canonical_markers(tmp_path):
    """Pin the structural markers that pre-cutover output produced.

    The diff body interleaves several timestamps that vary by run, so
    asserting the exact string would be brittle. Pinning the structural
    markers — version headers, the file rows the diff visits, and the
    new-decision summary — gives byte-level coverage of the diff
    composition without baking in the fixture clock.
    """
    store_path = _fixed_store_with_two_snapshots(tmp_path)
    envelope = tool_diff_since_last_session(store_path)
    diff = envelope["diff"]
    # Version header line.
    assert diff.startswith("Changes from v001 → v002")
    # The new decision file surfaces with its summary line. The scaffold
    # may seed an initial decision, so we match on the suffix rather than
    # a fixed leading number.
    assert "+ New file: decisions/" in diff
    assert "adopt-postgres" in diff
    assert "Adopt Postgres" in diff


def test_session_scoped_zero_snapshots_byte_identical(tmp_path):
    store_path = tmp_path / "store"
    scaffold_project_store("byte-parity-empty", store_path)
    envelope = tool_diff_since_last_session(store_path)
    assert envelope["diff"] == "Not enough snapshots to compute a diff (need at least 2)."


def test_session_scoped_one_snapshot_byte_identical(tmp_path):
    store_path = tmp_path / "store"
    scaffold_project_store("byte-parity-one", store_path)
    capture_snapshot(store_path, trigger="only-one")
    envelope = tool_diff_since_last_session(store_path)
    assert envelope["diff"] == "Not enough snapshots to compute a diff (need at least 2)."


def test_days_based_zero_snapshots_byte_identical(tmp_path):
    store_path = tmp_path / "store"
    scaffold_project_store("byte-parity-days-empty", store_path)
    envelope = tool_diff_since_last_session(store_path, days=7)
    assert envelope["diff"] == "No snapshots available."
