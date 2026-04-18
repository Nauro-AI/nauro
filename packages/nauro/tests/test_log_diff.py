"""Tests for nauro log, nauro diff, and store/reader.py."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import SNAPSHOTS_DIR
from nauro.store.reader import (
    diff_since_last_session,
    diff_snapshots,
    read_project_context,
)
from nauro.store.snapshot import capture_snapshot, find_snapshot_near_date, load_snapshot
from nauro.store.writer import append_decision, append_question, update_state
from nauro.templates.scaffolds import scaffold_project_store

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
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
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
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
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
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        append_decision(store, "Use Postgres")
        capture_snapshot(store, trigger="with decision")

        result = runner.invoke(app, ["log", "--full", "--limit", "1"])
        assert result.exit_code == 0
        assert "project.md" in result.output
        assert "state.md" in result.output
        assert "decisions/" in result.output

    def test_log_no_snapshots(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["log"])
        assert result.exit_code == 0
        assert "No snapshots" in result.output

    def test_log_no_project(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["log"])
        assert result.exit_code == 1
        assert "No project found" in result.output


# --- CLI: nauro diff tests ---


class TestDiffCommand:
    def test_diff_no_args(self, tmp_path: Path, monkeypatch):
        """nauro diff — diff since last session."""
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="first")
        append_decision(store, "Use Redis")
        capture_snapshot(store, trigger="second")

        result = runner.invoke(app, ["diff"])
        assert result.exit_code == 0
        assert "v001" in result.output
        assert "v002" in result.output

    def test_diff_one_version(self, tmp_path: Path, monkeypatch):
        """nauro diff <version> — diff that version against latest."""
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="v1")
        append_decision(store, "Decision A")
        capture_snapshot(store, trigger="v2")
        append_decision(store, "Decision B")
        capture_snapshot(store, trigger="v3")

        result = runner.invoke(app, ["diff", "1"])
        assert result.exit_code == 0
        assert "v001" in result.output
        assert "v003" in result.output

    def test_diff_two_versions(self, tmp_path: Path, monkeypatch):
        """nauro diff <a> <b> — diff between two specific versions."""
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="v1")
        append_decision(store, "Decision A")
        capture_snapshot(store, trigger="v2")
        append_decision(store, "Decision B")
        capture_snapshot(store, trigger="v3")

        result = runner.invoke(app, ["diff", "1", "2"])
        assert result.exit_code == 0
        assert "v001" in result.output
        assert "v002" in result.output
        # Should NOT mention v003
        assert "v003" not in result.output

    def test_diff_invalid_version(self, tmp_path: Path, monkeypatch):
        """nauro diff with invalid version shows error."""
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="v1")

        result = runner.invoke(app, ["diff", "999"])
        assert result.exit_code == 1

    def test_diff_no_project(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["diff"])
        assert result.exit_code == 1
        assert "No project found" in result.output

    def test_diff_not_enough_snapshots(self, tmp_path: Path, monkeypatch):
        """nauro diff with < 2 snapshots shows helpful message."""
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["diff"])
        assert result.exit_code == 0
        assert "Not enough snapshots" in result.output

    def test_diff_same_version(self, tmp_path: Path, monkeypatch):
        """nauro diff <latest> shows message that it's already latest."""
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="v1")

        result = runner.invoke(app, ["diff", "1"])
        assert result.exit_code == 0
        assert "already the latest" in result.output


# --- Helpers for time-based snapshot fixtures ---


def _backdate_snapshot(store_path: Path, version: int, days_ago: int) -> None:
    """Rewrite a snapshot's timestamp to be N days ago."""
    snap_path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
    data = load_snapshot(store_path, version)
    data["timestamp"] = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
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
        target = datetime.now(UTC) - timedelta(days=7)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 2

    def test_finds_nearest_before(self, timed_store: Path):
        # 10 days ago — should pick v001 (14 days ago), not v002 (7 days ago)
        target = datetime.now(UTC) - timedelta(days=10)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 1

    def test_falls_back_to_oldest(self, timed_store: Path):
        # 30 days ago — nothing that old, should return oldest (v001)
        target = datetime.now(UTC) - timedelta(days=30)
        result = find_snapshot_near_date(timed_store, target)
        assert result is not None
        assert result["version"] == 1

    def test_no_snapshots(self, store: Path):
        result = find_snapshot_near_date(store, datetime.now(UTC))
        assert result is None

    def test_target_in_future(self, timed_store: Path):
        # Future target — should pick latest (v003)
        target = datetime.now(UTC) + timedelta(days=1)
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


# --- CLI: nauro diff --since tests ---


class TestDiffSinceFlag:
    def test_since_7d(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="first")
        _backdate_snapshot(store, 1, days_ago=14)
        append_decision(store, "Decision A")
        capture_snapshot(store, trigger="second")

        result = runner.invoke(app, ["diff", "--since", "7d"])
        assert result.exit_code == 0
        assert "v001" in result.output
        assert "v002" in result.output

    def test_since_plain_number(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="first")
        _backdate_snapshot(store, 1, days_ago=14)
        append_decision(store, "Decision A")
        capture_snapshot(store, trigger="second")

        result = runner.invoke(app, ["diff", "--since", "7"])
        assert result.exit_code == 0
        assert "v001" in result.output

    def test_since_invalid_value(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        from nauro.store.registry import register_project

        store = register_project("myproj", [tmp_path])
        scaffold_project_store("myproj", store)
        monkeypatch.chdir(tmp_path)

        capture_snapshot(store, trigger="first")

        result = runner.invoke(app, ["diff", "--since", "abc"])
        assert result.exit_code != 0
