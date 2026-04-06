"""Tests for store writer, snapshot, and CLI commands (note, sync)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.reader import _list_decisions
from nauro.store.snapshot import capture_snapshot, list_snapshots, load_snapshot
from nauro.store.validator import validate_store
from nauro.store.writer import append_decision, append_question, update_state
from nauro.templates.scaffolds import (
    get_scaffolds,
    scaffold_project_store,
)

runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Pre-scaffolded project store in tmp_path."""
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


# --- Scaffold tests ---


def test_scaffold_creates_all_files(store: Path):
    assert (store / "project.md").exists()
    assert (store / "state.md").exists()
    assert (store / "stack.md").exists()
    assert (store / "open-questions.md").exists()
    assert (store / "decisions").is_dir()
    assert (store / "snapshots").is_dir()


def test_scaffold_uses_bracketed_prompts(store: Path):
    content = (store / "project.md").read_text()
    assert "[What this does in one sentence" in content
    assert "<!--" not in content  # no HTML comments


def test_scaffold_stack_has_example(store: Path):
    content = (store / "stack.md").read_text()
    assert "Chosen for:" in content
    assert "Rejected:" in content


def test_scaffold_creates_first_decision(store: Path):
    first = store / "decisions" / "001-initial-setup.md"
    assert first.exists()
    content = first.read_text()
    # Verify well-formed YAML frontmatter (starts with ---, has a closing ---)
    assert content.startswith("---\n"), "First decision must start with YAML frontmatter"
    fm_end = content.index("\n---\n", 4)
    frontmatter = content[4:fm_end]
    assert "status: accepted" in frontmatter
    assert "confidence: high" in frontmatter
    assert "# 001: Initial project setup" in content
    assert "## Rejected Alternatives" in content


def test_get_scaffolds_returns_dict():
    scaffolds = get_scaffolds()
    assert "project.md" in scaffolds
    assert "state.md" in scaffolds
    assert "stack.md" in scaffolds
    assert "open-questions.md" in scaffolds


def test_decision_template_exists():
    """Decision template still available in scaffolds (for nauro init)."""
    from nauro.templates.scaffolds import DECISION_TEMPLATE

    assert "date:" in DECISION_TEMPLATE
    assert "status:" in DECISION_TEMPLATE
    assert "confidence:" in DECISION_TEMPLATE


# --- Decision tests ---


def test_decision_first(store: Path):
    path = append_decision(store, "Use Postgres")
    assert path.name == "002-use-postgres.md"  # 001 is initial-setup
    content = path.read_text()
    assert "# 002 — Use Postgres" in content
    assert "**Confidence:** medium" in content


def test_decision_auto_increment(store: Path):
    append_decision(store, "First decision")
    path = append_decision(store, "Second decision")
    assert path.name == "003-second-decision.md"  # 001 is initial-setup


def test_decision_with_rationale_and_rejected(store: Path):
    path = append_decision(
        store,
        "Use Redis",
        rationale="Fast in-memory store",
        rejected=[
            {"alternative": "Memcached", "reason": "Less feature-rich"},
            {"alternative": "DynamoDB", "reason": "Too expensive"},
        ],
        confidence="high",
    )
    content = path.read_text()
    assert "## Decision" in content
    assert "Fast in-memory store" in content
    assert "## Rejected Alternatives" in content
    assert "### Memcached" in content
    assert "Less feature-rich" in content
    assert "### DynamoDB" in content
    assert "Too expensive" in content
    assert "**Confidence:** high" in content


def test_decision_with_extended_fields(store: Path):
    """New decision format includes type, reversibility, source, files_affected."""
    path = append_decision(
        store,
        "Switch to Postgres",
        rationale="Better JSON support",
        confidence="high",
        decision_type="data_model",
        reversibility="hard",
        files_affected=["src/db.py", "migrations/"],
        source="compaction (session abc123)",
    )
    content = path.read_text()
    assert "**Type:** data_model" in content
    assert "**Reversibility:** hard" in content
    assert "**Source:** compaction (session abc123)" in content
    assert "**Files affected:** src/db.py, migrations/" in content


def test_decision_minimal_fields(store: Path):
    """Decision with only required fields works (backwards compat)."""
    path = append_decision(store, "Simple decision")
    content = path.read_text()
    assert "# 002 — Simple decision" in content
    assert "**Confidence:** medium" in content
    assert "**Date:**" in content
    # Optional fields should not appear
    assert "**Type:**" not in content
    assert "**Reversibility:**" not in content
    assert "**Source:**" not in content
    assert "**Files affected:**" not in content


def test_decision_metadata_format(store: Path):
    path = append_decision(store, "Test metadata")
    content = path.read_text()
    assert "**Date:**" in content
    assert "**Confidence:** medium" in content


# --- Question tests ---


def test_question_append(store: Path):
    append_question(store, "Should we use GraphQL?")
    content = (store / "open-questions.md").read_text()
    assert "Should we use GraphQL?" in content
    assert "UTC]" in content


def test_question_multiple(store: Path):
    append_question(store, "First question?")
    append_question(store, "Second question?")
    content = (store / "open-questions.md").read_text()
    lines = [line for line in content.split("\n") if line.startswith("- [") and "UTC]" in line]
    assert len(lines) == 2
    # Newest first (inserted at top)
    assert "Second question?" in lines[0]


# --- State update tests ---


def test_update_state_adds_delta(store: Path):
    update_state(store, "Implemented auth module")
    content = (store / "state.md").read_text()
    assert "Implemented auth module" in content
    assert "(none yet)" not in content


def test_update_state_keeps_last_5(store: Path):
    for i in range(7):
        update_state(store, f"Task {i}")
    content = (store / "state.md").read_text()
    items = [line for line in content.split("\n") if line.startswith("- Task")]
    assert len(items) == 5
    # Most recent first
    assert "Task 6" in items[0]


def test_update_state_updates_last_synced(store: Path):
    update_state(store, "something")
    content = (store / "state.md").read_text()
    assert "*Last synced:" in content
    # Should have a UTC timestamp, not the scaffold date
    assert "UTC" in content


# --- Snapshot tests ---


def test_snapshot_capture(store: Path):
    version = capture_snapshot(store, trigger="test")
    assert version == 1
    snap = load_snapshot(store, 1)
    assert snap["trigger"] == "test"
    assert "project.md" in snap["files"]
    assert "state.md" in snap["files"]
    assert snap["schema_version"] == 1


def test_snapshot_has_new_fields(store: Path):
    version = capture_snapshot(store, trigger="test", trigger_detail="detail here")
    snap = load_snapshot(store, version)
    assert snap["trigger_detail"] == "detail here"
    assert "token_count" in snap
    assert isinstance(snap["token_count"], int)


def test_snapshot_auto_increment(store: Path):
    v1 = capture_snapshot(store, trigger="first")
    v2 = capture_snapshot(store, trigger="second")
    assert v1 == 1
    assert v2 == 2


def test_snapshot_list_metadata(store: Path):
    capture_snapshot(store, trigger="snap1")
    capture_snapshot(store, trigger="snap2")
    snaps = list_snapshots(store)
    assert len(snaps) == 2
    assert snaps[0]["version"] == 2  # most recent first
    assert snaps[1]["version"] == 1
    assert "files" not in snaps[0]  # metadata only
    assert "trigger_detail" in snaps[0]
    assert "token_count" in snaps[0]


def test_snapshot_list_metadata_has_new_fields(store: Path):
    capture_snapshot(store, trigger="snap1", trigger_detail="some detail")
    snaps = list_snapshots(store)
    assert snaps[0]["trigger_detail"] == "some detail"
    assert snaps[0]["token_count"] > 0


def test_snapshot_logarithmic_pruning_keeps_correct_per_bucket(tmp_path: Path):
    """Logarithmic spacing keeps correct snapshots per time bucket."""
    store_path = tmp_path / "projects" / "prunetest"
    scaffold_project_store("prunetest", store_path)
    snapshots_dir = store_path / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Use a fixed midday time so hour/minute offsets never cross day boundaries
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)

    version = 0
    snapshots_data = []

    # 3 snapshots in last 7 days (should all be kept)
    for i in range(3):
        version += 1
        ts = now - timedelta(days=i, minutes=i * 10)
        snapshots_data.append((ts, version))

    # 3 snapshots on the SAME day 10 days ago (should keep only newest)
    for i in range(3):
        version += 1
        ts = now - timedelta(days=10, minutes=i * 10)
        snapshots_data.append((ts, version))

    # 3 snapshots in the SAME week ~45 days ago (should keep only newest)
    for i in range(3):
        version += 1
        ts = now - timedelta(days=45, minutes=i * 10)
        snapshots_data.append((ts, version))

    # 3 snapshots in the SAME month ~200 days ago (should keep only newest)
    for i in range(3):
        version += 1
        ts = now - timedelta(days=200, minutes=i * 10)
        snapshots_data.append((ts, version))

    # Write all snapshots
    for ts, ver in snapshots_data:
        snap = {
            "version": ver,
            "timestamp": ts.isoformat(),
            "trigger": f"test-{ver}",
            "trigger_detail": "",
            "token_count": 100,
            "files": {"project.md": "test"},
        }
        path = snapshots_dir / f"v{ver:03d}.json"
        path.write_text(json.dumps(snap) + "\n")

    # Trigger pruning by capturing a new snapshot
    capture_snapshot(store_path, trigger="trigger-prune")

    remaining = list(snapshots_dir.glob("v*.json"))
    remaining_versions = sorted(int(f.stem[1:]) for f in remaining)

    # All 3 recent (last 7 days) should be kept
    for v in range(1, 4):
        assert v in remaining_versions, f"Recent snapshot v{v} should be kept"

    # Latest (the one we just captured) should be kept
    assert max(remaining_versions) == 13

    # Expected survivors: 3 recent + 1 daily + 1 weekly + 1 monthly + 1 latest = 7
    # (from 12 original + 1 new = 13 total, pruned to exactly 7)
    assert len(remaining) == 7, f"Expected 7 snapshots after pruning, got {len(remaining)}"


def test_snapshot_decision_adding_never_pruned(tmp_path: Path):
    """Snapshots where decisions/ count increased are never pruned."""
    store_path = tmp_path / "projects" / "pintest"
    scaffold_project_store("pintest", store_path)
    snapshots_dir = store_path / "snapshots"

    now = datetime.now(UTC)

    # Create old snapshots — some with increasing decision counts
    for i in range(20):
        decisions = {f"decisions/{j:03d}-d.md": "content" for j in range(1, i // 2 + 1)}
        files = {"project.md": "test", **decisions}
        snap = {
            "version": i + 1,
            "timestamp": (now - timedelta(days=200 + i)).isoformat(),
            "trigger": f"old-{i}",
            "trigger_detail": "",
            "token_count": 100,
            "files": files,
        }
        path = snapshots_dir / f"v{i + 1:03d}.json"
        path.write_text(json.dumps(snap) + "\n")

    # Trigger pruning
    capture_snapshot(store_path, trigger="trigger")

    remaining = list(snapshots_dir.glob("v*.json"))

    # All remaining snapshots should be valid JSON with a version field
    remaining_versions = sorted(int(f.stem[1:]) for f in remaining)
    for f in remaining:
        data = json.loads(f.read_text())
        assert "version" in data

    # Pinned snapshots (where decision count increased) should survive pruning.
    # Versions 3,5,7,9,11,13,15,17,19 added a decision vs their predecessor.
    pinned_versions = {3, 5, 7, 9, 11, 13, 15, 17, 19}
    assert pinned_versions.issubset(set(remaining_versions)), (
        f"Some pinned snapshots were pruned. Remaining: {remaining_versions}"
    )

    # At minimum, the latest should be there
    versions = sorted(int(f.stem[1:]) for f in remaining)
    assert 21 in versions  # the capture_snapshot we just did


def test_snapshot_pruning_few_is_noop(store: Path):
    """Pruning with <100 snapshots is effectively a no-op (all kept if within 7 days)."""
    for i in range(10):
        capture_snapshot(store, trigger=f"snap-{i}")
    snaps = list(store.glob("snapshots/v*.json"))
    assert len(snaps) == 10  # all kept (all within last 7 days)


def test_snapshot_all_pinned_no_excessive_pruning(tmp_path: Path):
    """Edge case: all snapshots are pinned — don't prune below the logarithmic floor."""
    store_path = tmp_path / "projects" / "allpin"
    scaffold_project_store("allpin", store_path)
    snapshots_dir = store_path / "snapshots"

    now = datetime.now(UTC)

    # Create 15 old snapshots, each with increasing decision count (all pinned)
    for i in range(15):
        decisions = {f"decisions/{j:03d}-d.md": "content" for j in range(1, i + 2)}
        files = {"project.md": "test", **decisions}
        snap = {
            "version": i + 1,
            "timestamp": (now - timedelta(days=200 + i)).isoformat(),
            "trigger": f"pinned-{i}",
            "trigger_detail": "",
            "token_count": 100,
            "files": files,
        }
        path = snapshots_dir / f"v{i + 1:03d}.json"
        path.write_text(json.dumps(snap) + "\n")

    capture_snapshot(store_path, trigger="latest")
    remaining = list(snapshots_dir.glob("v*.json"))

    # All pinned snapshots plus the latest should survive
    # At minimum 15 pinned + 1 latest = 16 (first snapshot has no predecessor so not pinned)
    # Actually version 2+ are all pinned (each has more decisions than previous)
    assert len(remaining) >= 15


def test_snapshot_load_missing(store: Path):
    with pytest.raises(FileNotFoundError):
        load_snapshot(store, 999)


def test_snapshot_includes_decisions(store: Path):
    # 001-initial-setup.md already exists from scaffold
    version = capture_snapshot(store, trigger="with-decision")
    snap = load_snapshot(store, version)
    assert any("decisions/" in k for k in snap["files"])


# --- Decision reader tests (new format) ---


def test_reader_parses_new_format(store: Path):
    """Reader can parse decisions written in the new metadata format."""
    append_decision(
        store,
        "Use Postgres",
        rationale="Better JSON support",
        confidence="high",
        decision_type="data_model",
        reversibility="hard",
        files_affected=["src/db.py", "migrations/"],
        source="compaction (session abc)",
    )
    decisions = _list_decisions(store)
    new_decision = [d for d in decisions if d["title"] == "Use Postgres"]
    assert len(new_decision) == 1
    d = new_decision[0]
    assert d["decision_type"] == "data_model"
    assert d["reversibility"] == "hard"
    assert d["source"] == "compaction (session abc)"
    assert d["files_affected"] == ["src/db.py", "migrations/"]
    assert d["confidence"] == "high"
    assert d["rationale"]  # Should have rationale from ## Decision section


def test_reader_backwards_compat_old_format(store: Path):
    """Reader can still parse old-format decisions (YAML frontmatter)."""
    # The scaffold creates 001-initial-setup.md in old format
    decisions = _list_decisions(store)
    initial = [d for d in decisions if d["num"] == 1]
    assert len(initial) == 1
    d = initial[0]
    assert d["title"]  # Should parse title
    # New fields default to None for old format
    assert d["decision_type"] is None
    assert d["reversibility"] is None
    assert d["source"] is None


def test_reader_roundtrip_new_format(store: Path):
    """Write a decision with all fields, read back, verify all preserved."""
    append_decision(
        store,
        "Switch to WebSocket",
        rationale="Real-time updates needed",
        rejected=[
            {"alternative": "SSE", "reason": "Limited browser support"},
            {"alternative": "Polling", "reason": "High latency"},
        ],
        confidence="high",
        decision_type="api_design",
        reversibility="moderate",
        files_affected=["src/api/ws.py"],
        source="commit",
    )
    decisions = _list_decisions(store)
    d = [d for d in decisions if d["title"] == "Switch to WebSocket"][0]
    assert d["confidence"] == "high"
    assert d["decision_type"] == "api_design"
    assert d["reversibility"] == "moderate"
    assert d["source"] == "commit"
    assert "src/api/ws.py" in d["files_affected"]
    # Content should contain the rejected alternatives
    assert "### SSE" in d["content"]
    assert "### Polling" in d["content"]


# --- Question parser tests ---


# --- Validation tests ---


def test_validate_unfilled_prompts(store: Path):
    warnings = validate_store(store)
    # Fresh scaffold should have unfilled bracket prompts
    prompt_warnings = [w for w in warnings if "unfilled prompt" in w]
    assert len(prompt_warnings) > 0


def test_validate_stale_sync(tmp_path: Path):
    store = tmp_path / "projects" / "stale"
    scaffold_project_store("stale", store)
    # Make Last synced old
    state = store / "state.md"
    old_date = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d")
    state.write_text(f"# Current State\n*Last synced: {old_date}*\n")
    warnings = validate_store(store)
    stale_warnings = [w for w in warnings if "days ago" in w]
    assert len(stale_warnings) == 1


def test_validate_decision_gap(store: Path):
    # Create decisions 001 (exists from scaffold), 002, then 004 (gap at 003)
    append_decision(store, "Second")
    # Manually create 004 to create a gap
    (store / "decisions" / "004-skipped.md").write_text("# 004: Skipped\n")
    warnings = validate_store(store)
    gap_warnings = [w for w in warnings if "gap" in w]
    assert len(gap_warnings) == 1
    assert "3" in gap_warnings[0]


def test_validate_no_warnings_clean_store(tmp_path: Path):
    """A store with filled prompts and recent sync has no warnings."""
    store = tmp_path / "projects" / "clean"
    store.mkdir(parents=True)
    (store / "decisions").mkdir()
    (store / "snapshots").mkdir()

    now = datetime.now(UTC)
    (store / "project.md").write_text("# Clean\n\nA clean project.\n")
    (store / "state.md").write_text(
        f"# Current State\n*Last synced: {now.strftime('%Y-%m-%d %H:%M UTC')}*\n"
    )
    (store / "stack.md").write_text("# Stack\n- Python 3.11\n")
    (store / "open-questions.md").write_text("# Open Questions\n")
    (store / "decisions" / "001-init.md").write_text("# 001: Init\n")

    warnings = validate_store(store)
    assert len(warnings) == 0


# --- CLI: note command ---


def test_note_decision_cli(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Use Postgres for storage"])
    assert result.exit_code == 0
    assert "Decision recorded" in result.output
    assert "002-use-postgres-for-storage.md" in result.output


def test_note_question_auto_detect(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Should we use GraphQL?"])
    assert result.exit_code == 0
    assert "Question added" in result.output


def test_note_question_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "--question", "This is a question"])
    assert result.exit_code == 0
    assert "Question added" in result.output


def test_note_no_project(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "something"])
    assert result.exit_code == 1
    assert "No project found" in result.output


# --- CLI: sync command ---


def test_sync_cli(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "Synced myproj" in result.output
    assert "v001" in result.output


def test_sync_with_message(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["sync", "--message", "milestone release"])
    assert result.exit_code == 0

    # Verify snapshot has the trigger message
    snap = load_snapshot(store, 1)
    assert snap["trigger"] == "milestone release"


def test_sync_no_project(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "No project found" in result.output


def test_concurrent_decision_numbering(tmp_path: Path):
    """Two threads writing decisions simultaneously must produce unique sequential numbers."""
    import threading

    store_path = tmp_path / "projects" / "conctest"
    store_path.mkdir(parents=True, exist_ok=True)
    (store_path / "decisions").mkdir()

    errors: list[Exception] = []
    paths: list[Path] = []
    lock = threading.Lock()

    def write_decision(n: int) -> None:
        try:
            p = append_decision(store_path, f"Decision {n}", rationale="concurrent write test")
            with lock:
                paths.append(p)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=write_decision, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent writes raised exceptions: {errors}"
    assert len(paths) == 10, f"Expected 10 decisions, got {len(paths)}"

    # All file stems must be unique (no collisions)
    stems = [p.stem for p in paths]
    assert len(set(stems)) == 10, f"Duplicate decision filenames: {stems}"

    # Numbers must be 1..10 with no gaps
    nums = sorted(int(s.split("-")[0]) for s in stems)
    assert nums == list(range(1, 11)), f"Non-sequential decision numbers: {nums}"
