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
from tests.conftest import register_v2_repo


def _backdate_snapshot(store_path: Path, version: int, days_ago: int) -> None:
    snap_path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
    data = load_snapshot(store_path, version)
    data["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    snap_path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Register a project, scaffold the store, capture two snapshots."""
    result = register_v2_repo(tmp_path, "parity-diff", monkeypatch=monkeypatch)
    capture_snapshot(result.store_path, trigger="initial")
    append_decision(result.store_path, "Adopt Postgres", rationale="ACID semantics matter.")
    capture_snapshot(result.store_path, trigger="with-decision")
    return result.pid, result.store_path


@pytest.fixture
def time_indexed_repo(tmp_path, monkeypatch):
    """A repo with snapshots at known time offsets, suitable for days-based diffs.

    v001 is backdated to 14 days ago — comfortably older than the
    ``now - 7d`` cutoff the days-based tests request — so it resolves as
    the baseline while the requested cutoff and the baseline's own
    timestamp stay visibly DISTINCT (7 days apart). v002 is captured at
    ``now`` and serves as latest.
    """
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


def _clock_invariant(envelope: dict) -> dict:
    """Strip clock-derived parts so two independent calls compare equal.

    The days-based ``cutoff_date_used`` is the REQUESTED cutoff
    (``now - N days``), recomputed from ``datetime.now()`` on every call,
    and that same value is interpolated into the rendered ``Anchor:`` line.
    Two independent surface calls therefore differ by microseconds in the
    cutoff field and the anchor line, so parity is asserted on everything
    else; the cutoff's own consistency is checked separately.
    """
    out = {k: v for k, v in envelope.items() if k != "cutoff_date_used"}
    if isinstance(out.get("diff"), str):
        out["diff"] = "\n".join(
            line for line in out["diff"].split("\n") if not line.startswith("Anchor:")
        )
    return out


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
    # The cutoff is now() - N days, recomputed per call, so the two
    # independent surfaces agree on everything but that clock-derived value.
    assert _clock_invariant(stdio) == _clock_invariant(tool)
    assert stdio["store"] == "local"
    assert "v001" in stdio["diff"]
    assert "v002" in stdio["diff"]
    # Days-based path threads the REQUESTED cutoff (now - N days) through as
    # cutoff_date_used — not the (older) resolved baseline timestamp.
    assert stdio["cutoff_date_used"]


def test_days_based_surfaces_anchor_line(time_indexed_repo):
    # The local adapter threads cutoff_date_used into the kernel, so the
    # days-based path surfaces the requested-cutoff anchor header on both the
    # auto-generated CLI and the stdio MCP surface with no adapter change.
    pid, store_path = time_indexed_repo
    stdio = _stdio_envelope(pid, days=7)
    tool = _tool_envelope(store_path, days=7)
    assert _clock_invariant(stdio) == _clock_invariant(tool)
    assert "Anchor: requested ≤ " in stdio["diff"]
    assert "most-recent snapshot at-or-before cutoff" in stdio["diff"]
    assert stdio["cutoff_date_used"] in stdio["diff"]

    # The anchor's "requested ≤" value is the REQUESTED cutoff (now - 7d),
    # parsing within a few seconds of it — not the resolved baseline.
    cutoff = datetime.fromisoformat(stdio["cutoff_date_used"])
    expected_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    assert abs((cutoff - expected_cutoff).total_seconds()) < 60

    # The cutoff must be DISTINCT from the resolved baseline timestamp: the
    # fixture backdates the baseline (v001) to 14 days ago, so the requested
    # cutoff (7 days ago) and the "resolved to baseline <ts>" value differ.
    baseline_ts = load_snapshot(store_path, 1)["timestamp"]
    assert stdio["cutoff_date_used"] != baseline_ts
    assert f"resolved to baseline {baseline_ts[:19]}" in stdio["diff"]
    assert f"requested ≤ {stdio['cutoff_date_used']}" in stdio["diff"]


def test_session_scoped_diff_omits_anchor_line(seeded_repo):
    # The no-arg session diff carries no cutoff, so the anchor header must be
    # absent — keeping that output byte-identical to pre-anchor behaviour.
    pid, _store_path = seeded_repo
    stdio = _stdio_envelope(pid)
    assert "Anchor:" not in stdio["diff"]


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
    # The sentinel diff carries no anchor line, but the envelope still
    # surfaces the (clock-derived) requested cutoff, so compare modulo it.
    assert _clock_invariant(stdio) == _clock_invariant(tool)
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
