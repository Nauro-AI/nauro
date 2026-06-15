"""Tests for nauro log, nauro diff, and the diff kernel + adapter wiring."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nauro_core.operations import diff_since_last_session as _diff_since_last_session_op
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations import update_state as _update_state_op
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import SNAPSHOTS_DIR
from nauro.mcp.tools import tool_diff_since_last_session
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.snapshot import (
    capture_snapshot,
    find_snapshot_near_date,
    load_snapshot,
)
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision
from tests.conftest import read_project_context


def update_state(store_path: Path, delta: str) -> None:
    """Thin wrapper preserving the pre-cutover ``writer.update_state`` shape."""
    _update_state_op(FilesystemStore(store_path), delta)


def append_question(store_path: Path, question: str) -> None:
    """Thin wrapper preserving the pre-cutover ``writer.append_question`` shape."""
    _flag_question_op(FilesystemStore(store_path), question, None)


def diff_since_last_session(store_path: Path, days: int | None = None) -> str:
    """Test shim — return the diff string from the local MCP adapter.

    Goes through ``tool_diff_since_last_session`` so the existing
    string-assertion tests exercise the same adapter the CLI and MCP
    surfaces use. The envelope shape itself is pinned separately by the
    parity tests.
    """
    envelope = tool_diff_since_last_session(store_path, days)
    return envelope.get("diff") or ""


def diff_snapshots(store_path: Path, version_a: int, version_b: int) -> str:
    """Test shim — mirror the pre-cutover two-version diff helper.

    Loads both versions from disk (raising ``FileNotFoundError`` to match
    the pre-cutover contract) and renders via the kernel. The two-version
    CLI path uses this same shape; surface tests pin it.
    """
    baseline = load_snapshot(store_path, version_a)
    latest = load_snapshot(store_path, version_b)
    result = _diff_since_last_session_op(FilesystemStore(store_path), baseline, latest)
    return result.diff or ""


runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Pre-scaffolded project store in tmp_path."""
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


@pytest.fixture
def evolved_store(store: Path) -> Path:
    """Store with multiple snapshots showing realistic evolution.

    Snapshot 1: Initial state (scaffold defaults, includes 001-initial-setup)
    Snapshot 2: First decision added, question asked, state updated
    Snapshot 3: Second decision, question resolved, stack updated, more state changes
    """
    # Snapshot 1: initial state
    capture_snapshot(store, trigger="initial sync")

    # --- Evolve: add decision, question, update state ---
    append_decision(
        store,
        "Use Postgres for storage",
        rationale="Mature, reliable RDBMS with great JSON support.",
        rejected=[
            {"alternative": "MongoDB", "reason": "No ACID guarantees for task state transitions."},
            {"alternative": "SQLite", "reason": "No concurrent writes for multi-user."},
        ],
        confidence="high",
    )
    append_question(store, "Should we add Redis for caching?")
    update_state(store, "Set up database schema")

    # Snapshot 2: after first round of changes
    capture_snapshot(store, trigger="post-db-setup")

    # --- Evolve more: another decision, resolve question, update stack ---
    append_decision(
        store,
        "Use Redis for caching",
        rationale="Low-latency in-memory store, good ecosystem.",
        rejected=[{"alternative": "Memcached", "reason": "Weaker data structures."}],
        confidence="medium",
    )

    # Update stack.md with tech choices
    stack_path = store / "stack.md"
    stack_path.write_text(
        "# Stack\n"
        "## Backend\n"
        "- Python 3.11 with FastAPI\n"
        "- PostgreSQL for persistence\n"
        "- Redis for caching\n"
    )

    # Resolve the question by removing it and adding a note
    oq_path = store / "open-questions.md"
    oq_path.write_text("# Open Questions\n[Append-only unresolved threads — newest first]\n")

    update_state(store, "Added caching layer with Redis")
    update_state(store, "Deployed v0.2.0 to staging")

    # Snapshot 3: after second round
    capture_snapshot(store, trigger="post-caching")

    return store


# --- read_project_context tests ---


class TestReadProjectContext:
    def test_l0_includes_state(self, store: Path):
        content = read_project_context(store, level=0)
        assert "Current State" in content

    def test_l0_includes_recent_decisions(self, evolved_store: Path):
        content = read_project_context(evolved_store, level=0)
        assert "Recent Decisions" in content
        assert "Use Redis for caching" in content

    def test_l0_limits_to_10_decisions(self, store: Path):
        for i in range(15):
            append_decision(store, f"Decision {i}")
        content = read_project_context(store, level=0)
        # L0 summary shows up to 10 most recent active decisions
        recent_section = content[content.index("## Recent Decisions") :]
        summary_lines = [line for line in recent_section.split("\n") if line.startswith("- D")]
        assert len(summary_lines) == 10
        assert "Decision 14" in content
        assert "Decision 5" in content

    def test_l1_includes_full_stack(self, evolved_store: Path):
        content = read_project_context(evolved_store, level=1)
        assert "Python 3.11 with FastAPI" in content
        assert "PostgreSQL for persistence" in content

    def test_l1_includes_decisions_with_rationale(self, evolved_store: Path):
        content = read_project_context(evolved_store, level=1)
        assert "Mature, reliable RDBMS" in content

    def test_l2_includes_all_decisions(self, store: Path):
        for i in range(15):
            append_decision(store, f"Decision {i}")
        content = read_project_context(store, level=2)
        assert "Decision 0" in content
        assert "Decision 14" in content


# --- diff_snapshots tests ---


class TestDiffSnapshots:
    def test_detects_new_decisions(self, evolved_store: Path):
        result = diff_snapshots(evolved_store, 1, 2)
        assert "v001" in result
        assert "v002" in result
        # Should mention the new decision file
        assert "decisions/" in result

    def test_detects_state_changes(self, evolved_store: Path):
        result = diff_snapshots(evolved_store, 1, 2)
        assert "state_current.md" in result

    def test_detects_resolved_questions(self, evolved_store: Path):
        result = diff_snapshots(evolved_store, 2, 3)
        assert "open-questions.md" in result
        assert "Resolved" in result or "removed" in result.lower()

    def test_detects_stack_changes(self, evolved_store: Path):
        result = diff_snapshots(evolved_store, 2, 3)
        assert "stack.md" in result
        assert "Python 3.11" in result or "PostgreSQL" in result or "Redis" in result

    def test_detects_new_question(self, evolved_store: Path):
        result = diff_snapshots(evolved_store, 1, 2)
        assert "question" in result.lower()

    def test_no_changes(self, store: Path):
        capture_snapshot(store, trigger="first")
        capture_snapshot(store, trigger="second")
        result = diff_snapshots(store, 1, 2)
        assert "No changes" in result

    def test_missing_version_raises(self, store: Path):
        capture_snapshot(store, trigger="first")
        with pytest.raises(FileNotFoundError):
            diff_snapshots(store, 1, 999)

    def test_missing_both_versions_raises(self, store: Path):
        with pytest.raises(FileNotFoundError):
            diff_snapshots(store, 100, 200)

    def test_version_order(self, evolved_store: Path):
        # Diffing in reverse should still work
        result = diff_snapshots(evolved_store, 3, 1)
        assert "v003" in result
        assert "v001" in result


# --- diff_since_last_session tests ---


class TestDiffSinceLastSession:
    def test_returns_diff_between_last_two(self, evolved_store: Path):
        result = diff_since_last_session(evolved_store)
        assert "v002" in result
        assert "v003" in result

    def test_insufficient_snapshots(self, store: Path):
        result = diff_since_last_session(store)
        assert "Not enough snapshots" in result

    def test_single_snapshot(self, store: Path):
        capture_snapshot(store, trigger="only one")
        result = diff_since_last_session(store)
        assert "Not enough snapshots" in result


# --- CLI: nauro log tests ---


class TestLogCommand:
    def test_log_shows_snapshots(self, tmp_path: Path, monkeypatch):
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="first sync")
        capture_snapshot(store, trigger="second sync")

        result = runner.invoke(app, ["log"])
        assert result.exit_code == 0
        assert "v001" in result.output
        assert "v002" in result.output
        assert "first sync" in result.output
        assert "second sync" in result.output

    def test_log_limit(self, tmp_path: Path, monkeypatch):
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        for i in range(5):
            capture_snapshot(store, trigger=f"snap-{i}")

        result = runner.invoke(app, ["log", "--limit", "2"])
        assert result.exit_code == 0
        assert "v005" in result.output
        assert "v004" in result.output
        assert "v001" not in result.output

    def test_log_full(self, tmp_path: Path, monkeypatch):
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        append_decision(store, "Use Postgres")
        capture_snapshot(store, trigger="with decision")

        result = runner.invoke(app, ["log", "--full", "--limit", "1"])
        assert result.exit_code == 0
        assert "project.md" in result.output
        assert "state_current.md" in result.output
        assert "decisions/" in result.output

    def test_log_no_snapshots(self, tmp_path: Path, monkeypatch):
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["log"])
        assert result.exit_code == 0
        assert "No snapshots" in result.output

    def test_log_no_project(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["log"])
        assert result.exit_code == 1
        assert "No project found" in result.output


# --- CLI: nauro diff tests ---


class TestDiffCommand:
    def test_diff_no_args(self, tmp_path: Path, monkeypatch):
        """nauro diff-since-last-session — diff since last session."""
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="first")
        append_decision(store, "Use Redis")
        capture_snapshot(store, trigger="second")

        result = runner.invoke(app, ["diff-since-last-session"])
        assert result.exit_code == 0
        envelope = json.loads(result.stdout)
        assert "v001" in envelope["diff"]
        assert "v002" in envelope["diff"]

    def test_diff_no_project(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["diff-since-last-session"])
        assert result.exit_code == 1
        assert "No project found" in result.output

    def test_diff_not_enough_snapshots(self, tmp_path: Path, monkeypatch):
        """nauro diff-since-last-session with < 2 snapshots shows helpful message."""
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["diff-since-last-session"])
        assert result.exit_code == 0
        envelope = json.loads(result.stdout)
        assert "Not enough snapshots" in envelope["diff"]

    def test_diff_registered_but_missing_store(self, tmp_path: Path, monkeypatch):
        """A registered project whose store directory was deleted must
        surface the WELCOME_NO_PROJECT guidance on stderr and exit
        nonzero. The auto-gen command routes through
        ``tool_diff_since_last_session`` which short-circuits with an
        error envelope when ``store_path.exists()`` is False; the CLI
        respects that envelope rather than swallowing it as an empty
        diff string.
        """
        import shutil

        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        # Delete the project store directory; the registry still points
        # at it, so resolve_target_project succeeds but the store is gone.
        shutil.rmtree(store)

        result = runner.invoke(app, ["diff-since-last-session"])
        assert result.exit_code == 1
        assert "Welcome to Nauro" in result.output
        assert "nauro init" in result.output


# --- Helpers for time-based snapshot fixtures ---


def _backdate_snapshot(store_path: Path, version: int, days_ago: int) -> None:
    """Rewrite a snapshot's timestamp to be N days ago."""
    snap_path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
    data = load_snapshot(store_path, version)
    data["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    snap_path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture
def timed_store(store: Path) -> Path:
    """Store with snapshots at known time offsets.

    v001: 14 days ago (initial)
    v002: 7 days ago (added decision)
    v003: now (added second decision)
    """
    capture_snapshot(store, trigger="initial sync")
    _backdate_snapshot(store, 1, days_ago=14)

    append_decision(store, "Use Postgres", rationale="Reliable RDBMS.")
    capture_snapshot(store, trigger="post-db")
    _backdate_snapshot(store, 2, days_ago=7)

    append_decision(store, "Use Redis", rationale="Fast cache.")
    capture_snapshot(store, trigger="post-cache")
    # v003 stays at current time

    return store


# --- find_snapshot_near_date tests ---


class TestFindSnapshotNearDate:
    def test_finds_exact_match(self, timed_store: Path):
        target = datetime.now(timezone.utc) - timedelta(days=7)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 2

    def test_finds_nearest_before(self, timed_store: Path):
        # 10 days ago — should pick v001 (14 days ago), not v002 (7 days ago)
        target = datetime.now(timezone.utc) - timedelta(days=10)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 1

    def test_falls_back_to_oldest(self, timed_store: Path):
        # 30 days ago — nothing that old, should return oldest (v001)
        target = datetime.now(timezone.utc) - timedelta(days=30)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 1

    def test_no_snapshots(self, store: Path):
        result = find_snapshot_near_date(store, datetime.now(timezone.utc))
        assert result is None

    def test_target_in_future(self, timed_store: Path):
        # Future target — should pick latest (v003)
        target = datetime.now(timezone.utc) + timedelta(days=1)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 3


# --- Time-based diff_since_last_session tests ---


class TestDiffSinceLastSessionTimeBased:
    def test_days_omitted_unchanged_behavior(self, evolved_store: Path):
        """days=None preserves original session-scoped behavior."""
        result = diff_since_last_session(evolved_store)
        assert "v002" in result
        assert "v003" in result

    def test_days_7_finds_correct_snapshot(self, timed_store: Path):
        """days=7 diffs v002 (7 days ago) against v003 (latest)."""
        result = diff_since_last_session(timed_store, days=7)
        assert "v002" in result
        assert "v003" in result
        # Should show the new decision added between v002 and v003
        assert "decisions/" in result

    def test_days_larger_than_available(self, timed_store: Path):
        """days=100 falls back to oldest snapshot (v001)."""
        result = diff_since_last_session(timed_store, days=100)
        assert "v001" in result
        assert "v003" in result

    def test_days_0_edge_case(self, timed_store: Path):
        """days=0 targets now — baseline is v003 (latest), same as latest → no diff."""
        result = diff_since_last_session(timed_store, days=0)
        assert "no diff" in result.lower() or "one snapshot" in result.lower()

    def test_days_1_edge_case(self, timed_store: Path):
        """days=1 targets yesterday — should pick v002 (7 days ago) as baseline."""
        result = diff_since_last_session(timed_store, days=1)
        # v002 is 7 days ago, which is before 1 day ago — so baseline is v002
        assert "v002" in result
        assert "v003" in result

    def test_no_snapshots_graceful(self, store: Path):
        """days=7 with no snapshots returns graceful message."""
        result = diff_since_last_session(store, days=7)
        assert "No snapshots" in result


# --- Corrupt-timestamp tolerance ---


def _corrupt_timestamp(store_path: Path, version: int, value: str = "not-a-date") -> None:
    """Rewrite a snapshot's timestamp to an unparseable value on disk."""
    snap_path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
    data = load_snapshot(store_path, version)
    data["timestamp"] = value
    snap_path.write_text(json.dumps(data, indent=2) + "\n")


def _snapshot_file(store_path: Path, version: int) -> Path:
    return store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"


class TestCorruptSnapshotTimestamp:
    def test_capture_snapshot_prunes_around_bad_timestamp(self, store: Path):
        """A snapshot with an unparseable timestamp does not break pruning of
        the valid ones, and the bad snapshot file is left on disk untouched."""
        capture_snapshot(store, trigger="first")
        _backdate_snapshot(store, 1, days_ago=400)
        append_decision(store, "Use Postgres", rationale="Reliable RDBMS.")
        capture_snapshot(store, trigger="second")
        _backdate_snapshot(store, 2, days_ago=200)

        # Corrupt v002's timestamp before the next capture triggers a prune.
        _corrupt_timestamp(store, 2)

        # A third capture prunes; the prune loop must skip v002 and still
        # process v001/v003 without raising.
        capture_snapshot(store, trigger="third")

        # The corrupted snapshot is neither deleted nor pruned.
        assert _snapshot_file(store, 2).exists()
        # The latest snapshot is always kept.
        assert _snapshot_file(store, 3).exists()

    def test_find_snapshot_near_date_ignores_bad_timestamp(self, timed_store: Path):
        """find_snapshot_near_date resolves among the valid snapshots and
        ignores one whose timestamp cannot be parsed."""
        # timed_store: v001 14d ago, v002 7d ago, v003 now.
        _corrupt_timestamp(timed_store, 2)

        # Target 7 days ago: v002 would have matched exactly, but it is now
        # unparseable, so the nearest valid snapshot at/before the target is
        # v001 (14 days ago).
        target = datetime.now(timezone.utc) - timedelta(days=7)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 1

    def test_find_snapshot_near_date_all_bad_returns_none(self, timed_store: Path):
        """When every snapshot has an unparseable timestamp, the scan resolves
        to no candidates and returns None."""
        for version in (1, 2, 3):
            _corrupt_timestamp(timed_store, version)

        result = find_snapshot_near_date(timed_store, datetime.now(timezone.utc))
        assert result is None

    def test_capture_snapshot_all_bad_does_not_crash(self, store: Path):
        """capture_snapshot prunes after writing; with only bad-timestamp
        prior snapshots the prune must no-op rather than raise."""
        capture_snapshot(store, trigger="first")
        capture_snapshot(store, trigger="second")
        _corrupt_timestamp(store, 1)
        _corrupt_timestamp(store, 2)

        # The new capture writes v003 then prunes; every prior snapshot has a
        # bad timestamp, so the prune skips them all and returns without error.
        version = capture_snapshot(store, trigger="third")
        assert version == 3
        assert _snapshot_file(store, 1).exists()
        assert _snapshot_file(store, 2).exists()
        assert _snapshot_file(store, 3).exists()
