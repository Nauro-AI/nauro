"""Tests for store writer, snapshot, and CLI commands (note, sync)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations import update_state as _update_state_op
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import SNAPSHOTS_DIR
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.reader import _list_decisions
from nauro.store.snapshot import capture_snapshot, list_snapshots, load_snapshot
from nauro.store.validator import validate_store
from nauro.templates.scaffolds import (
    get_scaffolds,
    scaffold_project_store,
)
from tests._writer_compat import append_decision
from tests.conftest import read_project_context


def update_state(store_path: Path, delta: str) -> None:
    """Thin wrapper around the kernel for the legacy test surface.

    Pre-cutover the tests called ``writer.update_state`` directly; the
    write path now lives in :mod:`nauro_core.operations.update_state`.
    Kept as a helper to preserve the test bodies verbatim.
    """
    _update_state_op(FilesystemStore(store_path), delta)


def append_question(store_path: Path, question: str) -> None:
    """Thin wrapper around the kernel for the legacy test surface.

    Pre-cutover the tests called ``writer.append_question`` directly; the
    write path now lives in :mod:`nauro_core.operations.flag_question`.
    """
    _flag_question_op(FilesystemStore(store_path), question, None)


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
    assert (store / "state_current.md").exists()
    assert not (store / "state.md").exists()
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
    # Well-formed v2 YAML frontmatter.
    assert content.startswith("---\n"), "First decision must start with YAML frontmatter"
    fm_end = content.index("\n---\n", 4)
    frontmatter = content[4:fm_end]
    assert "status: active" in frontmatter
    assert "confidence: high" in frontmatter
    assert "# 001 \u2014 Initial project setup" in content
    assert "## Rejected Alternatives" in content


def test_get_scaffolds_returns_dict():
    scaffolds = get_scaffolds()
    assert "project.md" in scaffolds
    assert "state_current.md" in scaffolds
    assert "state.md" not in scaffolds
    assert "stack.md" in scaffolds
    assert "open-questions.md" in scaffolds


def test_scaffolded_first_decision_parses_as_v2(store: Path):
    """The scaffolded first decision round-trips through parse_decision."""
    from nauro_core.decision_model import parse_decision

    first = store / "decisions" / "001-initial-setup.md"
    d = parse_decision(first.read_text(), first.name)
    assert d.num == 1
    assert d.title == "Initial project setup"
    assert d.confidence.value == "high"
    assert d.status.value == "active"
    assert len(d.rejected) == 2


# --- Decision tests ---


def test_decision_first(store: Path):
    path = append_decision(store, "Use Postgres")
    assert path.name == "002-use-postgres.md"  # 001 is initial-setup
    content = path.read_text()
    assert "# 002 \u2014 Use Postgres" in content
    assert "confidence: medium" in content


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
    assert "confidence: high" in content


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
        source="compaction",
    )
    content = path.read_text()
    assert "decision_type: data_model" in content
    assert "reversibility: hard" in content
    assert "source: compaction" in content
    assert "src/db.py" in content
    assert "migrations/" in content


def test_decision_minimal_fields(store: Path):
    """Decision with only required fields works."""
    path = append_decision(store, "Simple decision")
    content = path.read_text()
    assert "# 002 \u2014 Simple decision" in content
    assert "confidence: medium" in content
    assert "date:" in content
    # Optional fields are present with null values (YAML frontmatter contract).
    assert "decision_type: null" in content
    assert "reversibility: null" in content
    assert "source: null" in content
    assert "files_affected: []" in content


def test_decision_metadata_format(store: Path):
    path = append_decision(store, "Test metadata")
    content = path.read_text()
    assert "date:" in content
    assert "confidence: medium" in content


# --- Question tests ---


def test_question_append(store: Path):
    append_question(store, "Should we use GraphQL?")
    content = (store / "open-questions.md").read_text()
    assert "Should we use GraphQL?" in content
    assert "[Q1]" in content


def test_question_multiple(store: Path):
    append_question(store, "First question?")
    append_question(store, "Second question?")
    content = (store / "open-questions.md").read_text()
    lines = [line for line in content.split("\n") if line.startswith("- [Q")]
    assert len(lines) == 2
    # Newest first (inserted at top); ids increment sequentially.
    assert "[Q2]" in lines[0]
    assert "Second question?" in lines[0]
    assert "[Q1]" in lines[1]
    assert "First question?" in lines[1]


# --- State update tests ---


def test_update_state_creates_state_current(store: Path):
    """update_state writes to state_current.md (scaffolded directly)."""
    update_state(store, "Implemented auth module")
    current = (store / "state_current.md").read_text()
    assert "# Current State" in current
    assert "Implemented auth module" in current
    # Scaffold no longer writes a legacy state.md.
    assert not (store / "state.md").exists()


def test_update_state_appends_history(store: Path):
    update_state(store, "First task")
    update_state(store, "Second task")
    current = (store / "state_current.md").read_text()
    assert "Second task" in current
    assert "First task" not in current
    # History file should contain the first task's state
    history = (store / "state_history.md").read_text()
    assert "First task" in history


def test_update_state_history_accumulates(store: Path):
    for i in range(5):
        update_state(store, f"Task {i}")
    current = (store / "state_current.md").read_text()
    assert "Task 4" in current
    history = (store / "state_history.md").read_text()
    # All previous tasks are in history (via rotation chain)
    for i in range(4):
        assert f"Task {i}" in history


def test_update_state_migration_preserves_legacy(tmp_path: Path):
    """Legacy stores: state.md only → first update_state migrates to state_current.md."""
    store = tmp_path / "legacy-store"
    (store / "decisions").mkdir(parents=True)
    (store / "snapshots").mkdir()
    (store / "state.md").write_text("# State\n\n## Current\nLegacy content\n\n## History\n")
    assert not (store / "state_current.md").exists()
    update_state(store, "Post-upgrade task")
    assert (store / "state_current.md").exists()
    assert (store / "state.md").exists()  # not deleted


def test_update_state_first_write_empty_store(tmp_path: Path):
    """First write to a store with no prior state files at all."""
    store_path = tmp_path / "empty-store"
    store_path.mkdir(parents=True)
    # No state.md or state_current.md
    update_state(store_path, "Brand new state")
    # Nothing should be created since there's no existing state file
    assert not (store_path / "state_current.md").exists()


# --- Reader split-state tests ---


def test_l2_loads_state_history(store: Path):
    """L2 context includes state_history.md content."""
    (store / "state_current.md").write_text("# Current State\n\nCurrent")
    (store / "state_history.md").write_text("## 2026-04-01T10:00Z\n\nOld\n\n---\n")
    result = read_project_context(store, level=2)
    assert "Current" in result
    assert "Old" in result


def test_l0_does_not_load_state_history(store: Path):
    """L0 context does not include state_history.md."""
    (store / "state_current.md").write_text("# Current State\n\nCurrent only")
    (store / "state_history.md").write_text("## 2026-04-01T10:00Z\n\nShould not appear\n\n---\n")
    result = read_project_context(store, level=0)
    assert "Current only" in result
    assert "Should not appear" not in result


def test_l1_does_not_load_state_history(store: Path):
    """L1 context does not include state_history.md."""
    (store / "state_current.md").write_text("# Current State\n\nCurrent only")
    (store / "state_history.md").write_text("## 2026-04-01T10:00Z\n\nShould not appear\n\n---\n")
    result = read_project_context(store, level=1)
    assert "Current only" in result
    assert "Should not appear" not in result


# --- Snapshot tests ---


def test_snapshot_capture(store: Path):
    version = capture_snapshot(store, trigger="test")
    assert version == 1
    snap = load_snapshot(store, 1)
    assert snap["trigger"] == "test"
    assert "project.md" in snap["files"]
    assert "state_current.md" in snap["files"]
    assert snap["schema_version"] == 1


def test_snapshot_has_new_fields(store: Path):
    version = capture_snapshot(store, trigger="test", trigger_detail="detail here")
    snap = load_snapshot(store, version)
    assert snap["trigger_detail"] == "detail here"
    assert "token_count" in snap
    assert isinstance(snap["token_count"], int)


def test_snapshot_capture_round_trips_canonical_fields(store: Path):
    # A local capture carries the full canonical field set: the snapshot
    # schema version, the trigger detail, a derived token count, and a
    # dense integer version.
    version = capture_snapshot(store, trigger="sync", trigger_detail="auto")
    snap = load_snapshot(store, version)
    assert snap["schema_version"] == 1
    assert snap["trigger_detail"] == "auto"
    assert isinstance(snap["token_count"], int)
    assert snap["version"] == version
    assert isinstance(snap["version"], int)


def test_snapshot_on_disk_key_order_matches_local_shape(store: Path):
    # The on-disk JSON key set and order must match the pre-serializer
    # local shape so existing local snapshots stay byte-compatible; this
    # is why the snapshot schema stays at 1 rather than bumping to 2.
    version = capture_snapshot(store, trigger="sync", trigger_detail="auto")
    raw = json.loads((store / SNAPSHOTS_DIR / f"v{version:03d}.json").read_text())
    assert list(raw.keys()) == [
        "schema_version",
        "version",
        "timestamp",
        "trigger",
        "trigger_detail",
        "token_count",
        "files",
    ]


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
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

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

    now = datetime.now(timezone.utc)

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

    now = datetime.now(timezone.utc)

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
    """Reader returns Decision objects with all fields populated."""
    append_decision(
        store,
        "Use Postgres",
        rationale="Better JSON support",
        confidence="high",
        decision_type="data_model",
        reversibility="hard",
        files_affected=["src/db.py", "migrations/"],
        source="compaction",
    )
    decisions = _list_decisions(store)
    new = [d for d in decisions if d.title == "Use Postgres"]
    assert len(new) == 1
    d = new[0]
    assert d.decision_type is not None and d.decision_type.value == "data_model"
    assert d.reversibility is not None and d.reversibility.value == "hard"
    assert d.source is not None and d.source.value == "compaction"
    assert d.files_affected == ["src/db.py", "migrations/"]
    assert d.confidence.value == "high"
    assert d.rationale  # from ## Decision section


def test_reader_scaffolded_first_decision(store: Path):
    """The scaffolded 001 decision parses cleanly; optional fields are None."""
    decisions = _list_decisions(store)
    initial = [d for d in decisions if d.num == 1]
    assert len(initial) == 1
    d = initial[0]
    assert d.title
    assert d.decision_type is None
    assert d.reversibility is None
    assert d.source is None


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
    d = next(d for d in decisions if d.title == "Switch to WebSocket")
    assert d.confidence.value == "high"
    assert d.decision_type is not None
    assert d.decision_type.value == "api_design"
    assert d.reversibility is not None
    assert d.reversibility.value == "moderate"
    assert d.source is not None
    assert d.source.value == "commit"
    assert "src/api/ws.py" in d.files_affected
    # Content should contain the rejected alternatives section
    assert "### SSE" in d.content
    assert "### Polling" in d.content


# --- FilesystemStore bulk read tests ---


def test_read_decisions_matches_serial_read(store: Path):
    """The bulk read returns, per stem, exactly what serial read_decision does."""
    append_decision(store, "Use Postgres")
    append_decision(store, "Use Redis")
    fs = FilesystemStore(store)
    stems = fs.list_decisions()
    bodies = fs.read_decisions(stems)
    assert [bodies[s] for s in stems] == [fs.read_decision(s) for s in stems]


def test_read_decisions_missing_stem_maps_to_none(store: Path):
    """A stem with no backing file maps to None, matching read_decision."""
    fs = FilesystemStore(store)
    bodies = fs.read_decisions(["999-vanished"])
    assert bodies == {"999-vanished": None}
    assert fs.read_decision("999-vanished") is None


def test_parse_all_decisions_bulk_equals_serial(store: Path):
    """parse_all_decisions over FilesystemStore yields the same decisions in the
    same order as reading and parsing each stem serially in list order."""
    from nauro_core.decision_model import parse_decision
    from nauro_core.operations.decision_lookup import parse_all_decisions

    append_decision(store, "Use Postgres")
    append_decision(store, "Use Redis")
    fs = FilesystemStore(store)

    serial = []
    for stem in fs.list_decisions():
        body = fs.read_decision(stem)
        if body is None:
            continue
        serial.append(parse_decision(body, f"{stem}.md"))

    bulk = parse_all_decisions(fs)
    assert [d.num for d in bulk] == [d.num for d in serial]
    assert [d.title for d in bulk] == [d.title for d in serial]


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
    # Validator prefers state_current.md (the default) for staleness check.
    state = store / "state_current.md"
    old_date = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    state.write_text(f"# Current State\n*Last synced: {old_date}*\n")
    warnings = validate_store(store)
    stale_warnings = [w for w in warnings if "days ago" in w]
    assert len(stale_warnings) == 1


def test_validate_decision_gap(store: Path):
    # Create decisions 001 (exists from scaffold), 002, then 004 (gap at 003).
    # The 004 filler is a minimal valid v2 decision so the reader can load it.
    append_decision(store, "Second")
    _minimal_v2_decision(
        store / "decisions" / "004-skipped.md",
        num=4,
        title="Skipped",
    )
    warnings = validate_store(store)
    gap_warnings = [w for w in warnings if "gap" in w]
    assert len(gap_warnings) == 1
    assert "3" in gap_warnings[0]


def test_validate_no_warnings_clean_store(tmp_path: Path):
    """A store with filled prompts and no issues has no warnings."""
    store = tmp_path / "projects" / "clean"
    store.mkdir(parents=True)
    (store / "decisions").mkdir()
    (store / "snapshots").mkdir()

    (store / "project.md").write_text("# Clean\n\nA clean project.\n")
    (store / "state.md").write_text("# State\n\n## Current\nShipping v1\n\n## History\n")
    (store / "stack.md").write_text("# Stack\n- Python 3.11\n")
    (store / "open-questions.md").write_text("# Open Questions\n")
    _minimal_v2_decision(
        store / "decisions" / "001-init.md",
        num=1,
        title="Init",
    )

    warnings = validate_store(store)
    assert len(warnings) == 0


def _clean_store_with_project_md(tmp_path: Path, project_md: str) -> Path:
    """Build a warning-free store whose project.md is the given content."""
    store = tmp_path / "projects" / "sized"
    store.mkdir(parents=True)
    (store / "decisions").mkdir()
    (store / "snapshots").mkdir()
    (store / "project.md").write_text(project_md)
    (store / "state.md").write_text("# State\n\n## Current\nShipping v1\n\n## History\n")
    (store / "stack.md").write_text("# Stack\n- Python 3.11\n")
    (store / "open-questions.md").write_text("# Open Questions\n")
    _minimal_v2_decision(store / "decisions" / "001-init.md", num=1, title="Init")
    return store


def test_validate_project_md_over_token_threshold_warns(tmp_path: Path):
    from nauro.constants import CHARS_PER_TOKEN, PROJECT_MD_TOKEN_WARN

    # One char over the threshold in estimated tokens.
    oversized = "# Big\n" + "x" * (PROJECT_MD_TOKEN_WARN * CHARS_PER_TOKEN)
    store = _clean_store_with_project_md(tmp_path, oversized)

    warnings = validate_store(store)
    size_warnings = [w for w in warnings if "estimated tokens" in w]
    assert len(size_warnings) == 1
    assert size_warnings[0].startswith("project.md:")
    assert f"{PROJECT_MD_TOKEN_WARN:,}" in size_warnings[0]
    assert "stack.md" in size_warnings[0]


def test_validate_project_md_at_token_threshold_is_silent(tmp_path: Path):
    from nauro.constants import CHARS_PER_TOKEN, PROJECT_MD_TOKEN_WARN

    # Exactly at the threshold: the warning fires only above it.
    at_threshold = "x" * (PROJECT_MD_TOKEN_WARN * CHARS_PER_TOKEN)
    store = _clean_store_with_project_md(tmp_path, at_threshold)

    warnings = validate_store(store)
    assert [w for w in warnings if "estimated tokens" in w] == []


def _minimal_v2_decision(path: Path, num: int, title: str) -> None:
    """Write a minimal valid v2 decision file for tests that just need one file."""
    path.write_text(
        "---\n"
        "date: 2026-04-17\n"
        "confidence: medium\n"
        "---\n\n"
        f"# {num:03d} \u2014 {title}\n\n"
        "## Decision\n\nPlaceholder rationale.\n"
    )


# --- CLI: note command ---


def test_note_decision_cli(tmp_path: Path, monkeypatch):
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Use Postgres for storage"])
    assert result.exit_code == 0
    assert "Decision recorded" in result.output
    assert "002-use-postgres-for-storage.md" in result.output


def test_note_question_auto_detect(tmp_path: Path, monkeypatch):
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Should we use GraphQL?"])
    assert result.exit_code == 0
    assert "Question added" in result.output


def test_note_question_flag(tmp_path: Path, monkeypatch):
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "--question", "This is a question"])
    assert result.exit_code == 0
    assert "Question added" in result.output


def test_note_no_project(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "something"])
    assert result.exit_code == 1
    assert "No project found" in result.output


# --- CLI: sync command ---


def test_sync_cli(tmp_path: Path, monkeypatch):
    from nauro.store.registry import register_project

    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "local-only project; nothing to upload" in result.output
    assert "v001" in result.output


def test_sync_with_message(tmp_path: Path, monkeypatch):
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
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "No project found" in result.output


# --- FilesystemStore path containment ---


def test_filesystem_store_write_file_rejects_traversal(tmp_path):
    """write_file fails loud on a traversal path rather than writing outside the
    store. Silently dropping or redirecting a write would corrupt the store, so
    an out-of-store path is an error, not a no-op."""
    store_root = tmp_path / "store"
    outside = tmp_path / "outside.md"
    with pytest.raises(ValueError):
        FilesystemStore(store_root).write_file("../outside.md", "pwned")
    assert not outside.exists()


def test_filesystem_store_delete_file_ignores_traversal(tmp_path):
    """delete_file never reaches outside the store; a traversal path is a no-op
    that leaves the sibling file intact."""
    store_root = tmp_path / "store"
    store_root.mkdir()
    victim = tmp_path / "victim.md"
    victim.write_text("keep me")
    FilesystemStore(store_root).delete_file("../victim.md")
    assert victim.read_text() == "keep me"


def test_filesystem_store_read_file_returns_none_on_traversal(tmp_path):
    """read_file treats an out-of-store path as absent rather than disclosing a
    file outside the project store."""
    store_root = tmp_path / "store"
    store_root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("top secret")
    assert FilesystemStore(store_root).read_file("../secret.md") is None
