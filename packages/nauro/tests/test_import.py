"""Tests for nauro import --memory-bank and --adr."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.commands.import_cmd import (
    _extract_adr_alternatives_strict,
    _extract_section,
    _import_adrs,
    _import_memory_bank,
    _import_progress,
)
from nauro.cli.main import app
from nauro.mcp.payloads import build_l0_payload
from nauro.store.registry import register_project_v2
from nauro.store.snapshot import list_snapshots
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Pre-scaffolded project store in tmp_path."""
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


@pytest.fixture
def full_memory_bank(tmp_path: Path) -> Path:
    """A complete .context/ directory with all Memory Bank files."""
    ctx = tmp_path / ".context"
    ctx.mkdir()

    (ctx / "projectBrief.md").write_text(
        "# Project Brief\n\nThis is a web app for task management.\n"
    )
    (ctx / "activeContext.md").write_text(
        "# Active Context\n\nCurrently building the auth module.\n"
    )
    (ctx / "techContext.md").write_text(
        "# Tech Context\n\nUsing React + Node.js with PostgreSQL.\n"
    )
    (ctx / "decisionLog.md").write_text(
        "# Decision Log\n\n"
        "## Decision: Use PostgreSQL\n"
        "We need ACID compliance and relational queries.\n\n"
        "## Decision: Use React for frontend\n"
        "Team has React experience, large ecosystem.\n"
    )
    (ctx / "progress.md").write_text(
        "# Progress\n\n"
        "- Set up project scaffolding\n"
        "- Implemented user registration\n"
        "- Added database migrations\n"
    )
    return ctx


@pytest.fixture
def partial_memory_bank(tmp_path: Path) -> Path:
    """A .context/ directory with only projectBrief.md."""
    ctx = tmp_path / ".context_partial"
    ctx.mkdir()
    (ctx / "projectBrief.md").write_text("# Project Brief\n\nA minimal project.\n")
    return ctx


# --- Full import ---


def test_import_complete_memory_bank(store: Path, full_memory_bank: Path):
    counts = _import_memory_bank(full_memory_bank, store)

    assert counts["files_merged"] == 3  # projectBrief, activeContext, techContext
    assert counts["decisions"] == 2
    assert counts["progress_items"] == 3

    # project.md should have imported content appended
    project_content = (store / "project.md").read_text()
    assert "## Imported from Memory Bank" in project_content
    assert "task management" in project_content

    # state_current.md should have the composed activeContext + progress
    # (the legacy "## Imported from Memory Bank" header is no longer added —
    # active body lands directly under prepare_state_update's "# Current State").
    state_current = (store / "state_current.md").read_text()
    assert "auth module" in state_current
    assert "## Recently completed" in state_current
    assert "Set up project scaffolding" in state_current

    # stack.md should have tech context
    stack_content = (store / "stack.md").read_text()
    assert "## Imported from Memory Bank" in stack_content
    assert "React + Node.js" in stack_content

    # decisions/ should have new files (001 from scaffold + 2 imported)
    decisions = sorted((store / "decisions").glob("*.md"))
    assert len(decisions) == 3
    assert "use-postgresql" in decisions[1].name
    assert "use-react-for-frontend" in decisions[2].name

    # Verify decision content
    pg_decision = decisions[1].read_text()
    assert "ACID compliance" in pg_decision


# --- Partial import ---


def test_import_partial_memory_bank(store: Path, partial_memory_bank: Path):
    counts = _import_memory_bank(partial_memory_bank, store)

    assert counts["files_merged"] == 1  # only projectBrief
    assert counts["decisions"] == 0
    assert counts["progress_items"] == 0

    # project.md should have imported content
    project_content = (store / "project.md").read_text()
    assert "## Imported from Memory Bank" in project_content
    assert "minimal project" in project_content

    # state_current.md should NOT have import header (no activeContext.md)
    state_content = (store / "state_current.md").read_text()
    assert "## Imported from Memory Bank" not in state_content


# --- Append, not overwrite ---


def test_import_preserves_existing_content(store: Path, full_memory_bank: Path):
    # Write some custom content to project.md first
    project_md = store / "project.md"
    original = project_md.read_text()
    assert "testproj" in original  # scaffold content present

    _import_memory_bank(full_memory_bank, store)

    updated = project_md.read_text()
    # Original scaffold content still present
    assert "testproj" in updated
    # Imported content appended after
    assert "## Imported from Memory Bank" in updated
    assert "task management" in updated
    # Original comes before the import header
    assert updated.index("testproj") < updated.index("## Imported from Memory Bank")


def test_import_preserves_existing_decisions(store: Path, full_memory_bank: Path):
    """Existing decisions (e.g., 001-initial-setup) are not overwritten."""
    initial = (store / "decisions" / "001-initial-setup.md").read_text()

    _import_memory_bank(full_memory_bank, store)

    # Original decision unchanged
    assert (store / "decisions" / "001-initial-setup.md").read_text() == initial
    # New decisions start at 002
    decisions = sorted((store / "decisions").glob("*.md"))
    assert decisions[0].name == "001-initial-setup.md"
    assert decisions[1].name.startswith("002-")


# --- Error cases ---


def test_import_nonexistent_directory_cli(tmp_path: Path, monkeypatch):
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import", "--memory-bank", "/nonexistent/path"])
    assert result.exit_code == 1
    assert "not a directory" in result.output


def test_import_directory_without_project_brief_cli(tmp_path: Path, monkeypatch):
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    empty_dir = tmp_path / "empty_context"
    empty_dir.mkdir()

    result = runner.invoke(app, ["import", "--memory-bank", str(empty_dir)])
    assert result.exit_code == 1
    assert "projectBrief.md" in result.output


# --- CLI integration ---


def test_import_cli_full(tmp_path: Path, monkeypatch, full_memory_bank: Path):
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import", "--memory-bank", str(full_memory_bank)])
    assert result.exit_code == 0
    assert "Imported Memory Bank into myproj" in result.output
    assert "3 file(s) merged" in result.output
    assert "2 decision(s) imported" in result.output
    assert "3 progress item(s) imported" in result.output

    # Verify snapshot was captured
    snaps = list_snapshots(store)
    assert len(snaps) >= 1
    assert snaps[0]["trigger"] == "import: memory-bank"


def test_import_cli_partial(tmp_path: Path, monkeypatch, partial_memory_bank: Path):
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import", "--memory-bank", str(partial_memory_bank)])
    assert result.exit_code == 0
    assert "1 file(s) merged" in result.output
    assert "0 decision(s) imported" in result.output


def test_import_cli_no_flags(tmp_path: Path, monkeypatch):
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import"])
    assert result.exit_code == 1
    assert "--memory-bank" in result.output


def test_import_cli_with_project_flag(tmp_path: Path, monkeypatch, full_memory_bank: Path):
    _pid, store_a = register_project_v2("proj_a", [tmp_path / "a"])
    scaffold_project_store("proj_a", store_a)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, ["import", "--memory-bank", str(full_memory_bank), "--project", "proj_a"]
    )
    assert result.exit_code == 0
    assert "proj_a" in result.output


# --- Edge cases ---


def test_import_decision_log_with_no_decisions(store: Path, tmp_path: Path):
    """decisionLog.md exists but has no ## Decision: blocks."""
    ctx = tmp_path / ".context_empty_decisions"
    ctx.mkdir()
    (ctx / "projectBrief.md").write_text("# Brief\n\nSomething.\n")
    (ctx / "decisionLog.md").write_text("# Decision Log\n\nNo decisions yet.\n")

    counts = _import_memory_bank(ctx, store)
    assert counts["decisions"] == 0


def test_import_progress_with_empty_items(store: Path, tmp_path: Path):
    """progress.md has empty list items that should be skipped."""
    ctx = tmp_path / ".context_empty_progress"
    ctx.mkdir()
    (ctx / "projectBrief.md").write_text("# Brief\n\nSomething.\n")
    (ctx / "progress.md").write_text("# Progress\n\n- \n- Real item\n-\n")

    counts = _import_memory_bank(ctx, store)
    assert counts["progress_items"] == 1


def test_import_empty_decision_heading_not_promoted_to_title(store: Path, tmp_path: Path):
    """A malformed empty '## Decision:' heading must not consume the following
    body line as its title or fabricate a phantom decision; only the real,
    titled decision is imported and the count stays honest."""
    ctx = tmp_path / ".context_empty_heading"
    ctx.mkdir()
    (ctx / "projectBrief.md").write_text("# Brief\n\nSomething.\n")
    (ctx / "decisionLog.md").write_text(
        "# Decision Log\n\n"
        "## Decision: Use Redis cache\n"
        "Fast in-memory store.\n\n"
        "## Decision: \n"
        "This line must not become a decision title.\n"
    )

    counts = _import_memory_bank(ctx, store)

    assert counts["decisions"] == 1
    decisions = sorted((store / "decisions").glob("*.md"))
    assert len(decisions) == 2  # scaffold 001 + the one real import

    # Collect every decision's title line ("# NNN — ..."); decision files lead
    # with YAML frontmatter, so the title is not the first line.
    title_lines = [
        ln
        for d in decisions
        for ln in d.read_text().splitlines()
        if ln.startswith("# ") and ln[2:3].isdigit()
    ]
    # The real decision keeps its title; the orphaned body line is never
    # promoted to any decision's title (it stays in the prior decision's body).
    assert any("Use Redis cache" in ln for ln in title_lines)
    assert not any("must not become a decision title" in ln for ln in title_lines)


# --- v2 split-state composition (single update_state call) ---


def _mb_with(tmp_path: Path, name: str, **files: str) -> Path:
    """Build a Memory Bank dir with the given files. Always includes projectBrief."""
    ctx = tmp_path / name
    ctx.mkdir()
    (ctx / "projectBrief.md").write_text("# Brief\n\nA test project.\n")
    for filename, content in files.items():
        (ctx / filename).write_text(content)
    return ctx


def test_active_context_lands_in_state_current_not_legacy(store: Path, tmp_path: Path):
    """Legacy stores may carry a state.md alongside state_current.md.
    Verify import writes only to state_current.md, never the legacy file."""
    legacy_marker = "# Legacy state — should not be touched\n"
    (store / "state.md").write_text(legacy_marker)
    mb = _mb_with(
        tmp_path,
        ".context_active_only",
        **{"activeContext.md": "# Active Context\n\nWiring up Stripe checkout.\n"},
    )

    _import_memory_bank(mb, store)

    state_current = (store / "state_current.md").read_text()
    assert "Wiring up Stripe checkout." in state_current
    # Legacy state.md remains untouched.
    assert (store / "state.md").read_text() == legacy_marker
    assert "Wiring up Stripe checkout." not in (store / "state.md").read_text()


def test_progress_items_compose_into_recently_completed(store: Path, tmp_path: Path):
    mb = _mb_with(
        tmp_path,
        ".context_progress_only",
        **{"progress.md": "# Progress\n\n- Item one\n- Item two\n- Item three\n"},
    )

    _import_memory_bank(mb, store)

    state_current = (store / "state_current.md").read_text()
    assert "## Recently completed" in state_current
    assert "- Item one" in state_current
    assert "- Item two" in state_current
    assert "- Item three" in state_current


def test_active_context_and_progress_compose_into_single_state_current(store: Path, tmp_path: Path):
    mb = _mb_with(
        tmp_path,
        ".context_both",
        **{
            "activeContext.md": "# Active Context\n\nReviewing payment flow.\n",
            "progress.md": "# Progress\n\n- A\n- B\n- C\n",
        },
    )

    _import_memory_bank(mb, store)

    state_current = (store / "state_current.md").read_text()
    assert "Reviewing payment flow." in state_current
    assert "## Recently completed" in state_current
    assert "- A" in state_current
    assert "- B" in state_current
    assert "- C" in state_current

    # state_history should NOT contain N entries (one per progress item).
    # update_state was called once, so at most one prior-state archive exists
    # (the migrated legacy scaffold).
    history_path = store / "state_history.md"
    if history_path.exists():
        # Count `## ` timestamp headers — there should be exactly one (or zero).
        history = history_path.read_text()
        timestamp_headers = [line for line in history.split("\n") if line.startswith("## 20")]
        assert len(timestamp_headers) <= 1


def test_build_l0_surfaces_imported_state(store: Path, tmp_path: Path):
    """Regression: imported activeContext + progress must reach the L0 payload.

    build_l0 passes include_history=False, so anything in state_history.md is
    invisible. This test fails if update_state is called per-progress-item
    (most items end up in history).
    """
    mb = _mb_with(
        tmp_path,
        ".context_l0",
        **{
            "activeContext.md": "# Active Context\n\nL0_ACTIVE_MARKER\n",
            "progress.md": "# Progress\n\n- L0_PROGRESS_MARKER\n",
        },
    )

    _import_memory_bank(mb, store)

    payload = build_l0_payload(store)
    assert "L0_ACTIVE_MARKER" in payload
    assert "L0_PROGRESS_MARKER" in payload


def test_progress_only_no_active_context(store: Path, tmp_path: Path):
    mb = _mb_with(
        tmp_path,
        ".context_progress_no_active",
        **{"progress.md": "# Progress\n\n- Only progress here\n"},
    )

    _import_memory_bank(mb, store)

    state_current = (store / "state_current.md").read_text()
    # State delta starts directly with the section header — no leading body.
    body_after_wrapper = state_current.split("# Current State", 1)[1].lstrip()
    assert body_after_wrapper.startswith("## Recently completed")


def test_active_only_no_progress(store: Path, tmp_path: Path):
    mb = _mb_with(
        tmp_path,
        ".context_active_no_progress",
        **{"activeContext.md": "# Active Context\n\nJust active body.\n"},
    )

    _import_memory_bank(mb, store)

    state_current = (store / "state_current.md").read_text()
    assert "Just active body." in state_current
    assert "## Recently completed" not in state_current


def test_neither_active_nor_progress_no_state_update(store: Path, tmp_path: Path):
    """Only projectBrief — no update_state call, scaffolded state_current.md untouched.

    When neither activeContext nor progress is imported, update_state is
    skipped entirely; the placeholder state_current.md from scaffold_project_store
    must remain unchanged.
    """
    state_before = (store / "state_current.md").read_text()
    mb = _mb_with(tmp_path, ".context_brief_only")  # only projectBrief.md

    _import_memory_bank(mb, store)

    assert (store / "state_current.md").read_text() == state_before
    assert not (store / "state.md").exists()


def test_import_progress_returns_parsed_items():
    items = _import_progress("# Progress\n\n- one\n- two\n- three\n")
    assert items == ["one", "two", "three"]


# ===========================================================================
# ADR import tests
# ===========================================================================


@pytest.fixture
def madr_directory(tmp_path: Path) -> Path:
    """Directory with 3 MADR-format ADR files."""
    adr_dir = tmp_path / "adrs"
    adr_dir.mkdir()

    (adr_dir / "0001-use-postgres.md").write_text(
        "# Use PostgreSQL as primary database\n\n"
        "## Context\n\n"
        "We need a relational database that supports ACID transactions.\n\n"
        "## Decision\n\n"
        "Use PostgreSQL for all persistent storage.\n\n"
        "## Consequences\n\n"
        "- Good, because it supports complex queries\n"
        "- Bad, because it requires more ops effort than SQLite\n"
    )
    (adr_dir / "0002-use-react.md").write_text(
        "# Use React for frontend\n\n"
        "## Context\n\n"
        "The team needs a frontend framework.\n\n"
        "## Decision\n\n"
        "Use React with TypeScript.\n\n"
        "## Consequences\n\n"
        "- Good, team already knows React\n"
    )
    (adr_dir / "0003-use-redis-for-caching.md").write_text(
        "# Use Redis for caching\n\n"
        "## Context\n\n"
        "API responses are slow without caching.\n\n"
        "## Decision\n\n"
        "Use Redis as a caching layer.\n\n"
        "## Rejected\n\n"
        "- Memcached\n"
        "- In-process cache\n\n"
        "## Consequences\n\n"
        "- Good, fast in-memory operations\n"
    )
    return adr_dir


@pytest.fixture
def nygard_directory(tmp_path: Path) -> Path:
    """Directory with Nygard-format ADR files."""
    adr_dir = tmp_path / "nygard_adrs"
    adr_dir.mkdir()

    (adr_dir / "001-use-microservices.md").write_text(
        "# 1. Use microservices architecture\n\n"
        "## Status\n\n"
        "Accepted\n\n"
        "## Context\n\n"
        "We need to scale different parts of the system independently.\n\n"
        "## Decision\n\n"
        "We will use a microservices architecture.\n\n"
        "## Consequences\n\n"
        "- We need service discovery\n"
        "- We rejected a monolith instead of microservices\n"
    )
    (adr_dir / "002-use-grpc.md").write_text(
        "# 2. Use gRPC for inter-service communication\n\n"
        "## Status\n\n"
        "Proposed\n\n"
        "## Context\n\n"
        "Services need to communicate efficiently.\n\n"
        "## Decision\n\n"
        "Use gRPC for all inter-service calls.\n\n"
        "## Consequences\n\n"
        "- Good, type-safe contracts via protobuf\n"
    )
    return adr_dir


def test_import_madr_format(store: Path, madr_directory: Path):
    """Test import of 3 MADR-format ADR files."""
    counts = _import_adrs(madr_directory, store)

    assert counts["imported"] == 3
    assert counts["skipped"] == 0

    # Check decisions were created (001 from scaffold + 3 imported)
    decisions = sorted((store / "decisions").glob("*.md"))
    assert len(decisions) == 4

    # Verify ordering preserved (postgres before react before redis)
    assert "use-postgresql" in decisions[1].name
    assert "use-react" in decisions[2].name
    assert "use-redis" in decisions[3].name

    # Verify content of first imported decision
    pg_content = decisions[1].read_text()
    assert "Use PostgreSQL as primary database" in pg_content
    assert "ACID transactions" in pg_content

    # Verify redis decision has rejected alternatives
    redis_content = decisions[3].read_text()
    assert "Memcached" in redis_content
    assert "In-process cache" in redis_content


def test_import_nygard_format(store: Path, nygard_directory: Path):
    """Test import of Nygard-format ADR files with status-based confidence."""
    counts = _import_adrs(nygard_directory, store)

    assert counts["imported"] == 2
    assert counts["skipped"] == 0

    decisions = sorted((store / "decisions").glob("*.md"))
    assert len(decisions) == 3  # 001 scaffold + 2 imported

    # First ADR has "Accepted" status → high confidence
    micro_content = decisions[1].read_text()
    assert "confidence: high" in micro_content
    # Title should have number prefix stripped
    assert "Use microservices architecture" in micro_content

    # Second ADR has "Proposed" status → medium confidence
    grpc_content = decisions[2].read_text()
    assert "confidence: medium" in grpc_content

    # Nygard consequences with "rejected" keyword should be extracted
    assert "monolith" in micro_content


@pytest.fixture
def h3_adr_directory(tmp_path: Path) -> Path:
    """Directory with an h3-heading ADR: h1 title, sections under ### headings."""
    adr_dir = tmp_path / "h3_adrs"
    adr_dir.mkdir()

    (adr_dir / "001-use-structured-logging.md").write_text(
        "# Use structured logging\n\n"
        "### Status\n\n"
        "Accepted\n\n"
        "### Context\n\n"
        "Plain-text logs are hard to query in aggregation tools.\n\n"
        "### Decision\n\n"
        "Emit logs as JSON with a stable field schema.\n\n"
        "### Consequences\n\n"
        "- Logs are machine-parseable\n"
        "- Local reading needs a pretty-printer\n"
    )
    return adr_dir


def test_import_h3_heading_adr(store: Path, h3_adr_directory: Path):
    """h3-heading ADRs (h1 title + ### sections) capture rationale and status.

    Before the fix ### sections were not matched, so these imported title-only
    with rationale None and default (medium) confidence.
    """
    counts = _import_adrs(h3_adr_directory, store)

    assert counts["imported"] == 1
    assert counts["skipped"] == 0

    decisions = sorted((store / "decisions").glob("*.md"))
    assert len(decisions) == 2  # 001 scaffold + 1 imported

    content = decisions[1].read_text()
    assert "Use structured logging" in content
    # Rationale captures both the ### Context and ### Decision bodies.
    assert "Plain-text logs are hard to query in aggregation tools." in content
    assert "Emit logs as JSON with a stable field schema." in content
    # ### Status "Accepted" maps to high confidence.
    assert "confidence: high" in content


def test_extract_section_mixed_h2_context_h3_decision():
    """A mixed-level ADR extracts an h3 subsection heading as its own section.

    The h3 heading is independently extractable (was None before the fix),
    while an h3 nested under an h2 stays inside the h2 body (unchanged
    boundary at level 2).
    """
    content = (
        "# Mixed heading levels\n\n"
        "## Context\n\n"
        "The context section text.\n\n"
        "### Decision\n\n"
        "The decision section text.\n"
    )
    assert _extract_section(content, "Decision") == "The decision section text."

    context = _extract_section(content, "Context")
    assert context is not None
    assert context.startswith("The context section text.")
    assert "### Decision" in context


def test_extract_section_prefers_h2_over_earlier_h3():
    """When a heading name exists at both h3 and h2, the h2 body wins even if
    the h3 appears first, so h2 extraction is byte-identical to the h2-only rule.
    """
    content = (
        "# Title\n\n"
        "## Decision\n\n"
        "Use the daemon.\n\n"
        "### Context\n\n"
        "A nested detail that must not shadow the top-level section.\n\n"
        "## Context\n\n"
        "The real top-level context body.\n"
    )
    assert _extract_section(content, "Context") == "The real top-level context body."


def test_import_skips_non_adr_markdown(store: Path, tmp_path: Path):
    """Test that non-ADR markdown files in the directory are skipped."""
    adr_dir = tmp_path / "mixed_dir"
    adr_dir.mkdir()

    # ADR file
    (adr_dir / "001-real-adr.md").write_text(
        "# Use something\n\n## Context\n\nSome context.\n\n## Decision\n\nSome decision.\n"
    )
    # Non-ADR files (no NNN- prefix)
    (adr_dir / "README.md").write_text("# README\n\nThis is not an ADR.\n")
    (adr_dir / "template.md").write_text("# Template\n\nADR template.\n")
    (adr_dir / "index.md").write_text("# Index\n\n- ADR 1\n")

    counts = _import_adrs(adr_dir, store)

    assert counts["imported"] == 1
    assert counts["skipped"] == 0

    decisions = sorted((store / "decisions").glob("*.md"))
    assert len(decisions) == 2  # 001 scaffold + 1 imported


def test_import_adr_empty_directory(store: Path, tmp_path: Path):
    """Test import from empty directory."""
    empty_dir = tmp_path / "empty_adrs"
    empty_dir.mkdir()

    counts = _import_adrs(empty_dir, store)

    assert counts["imported"] == 0
    assert counts["skipped"] == 0

    # Only scaffold decision should exist
    decisions = sorted((store / "decisions").glob("*.md"))
    assert len(decisions) == 1


def test_import_adr_cli_integration(tmp_path: Path, monkeypatch, madr_directory: Path):
    """Test ADR import via CLI."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import", "--adr", str(madr_directory)])
    assert result.exit_code == 0
    assert "Imported ADRs into myproj" in result.output
    assert "3 ADR(s) imported" in result.output
    assert "0 ADR(s) skipped" in result.output

    # Verify snapshot was captured
    snaps = list_snapshots(store)
    assert len(snaps) >= 1
    assert snaps[0]["trigger"] == "import: adr"


# --- Hardening: non-UTF-8 sources and honest zero-import warnings ---


def test_import_adr_non_utf8_does_not_crash(store: Path, tmp_path: Path):
    """A legacy-encoded byte in an ADR file must not abort the migration."""
    adr_dir = tmp_path / "legacy_adrs"
    adr_dir.mkdir()
    # Valid ADR structure with a lone 0xe9 ("é" in latin-1) in the body.
    (adr_dir / "0001-legacy.md").write_bytes(
        b"# Use a legacy thing\n\n## Context\n\nThe caf" + b"\xe9" + b" service is slow.\n"
    )

    counts = _import_adrs(adr_dir, store)
    assert counts["imported"] == 1  # no UnicodeDecodeError; the ADR imports


def test_import_memory_bank_unparsed_decisionlog_warns(tmp_path: Path, monkeypatch):
    """A non-empty decisionLog in Cline's native (non '## Decision:') format
    imports zero decisions but must say so, not report a silent success."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    mb = tmp_path / ".context_native"
    mb.mkdir()
    (mb / "projectBrief.md").write_text("# Brief\n\nA project.\n")
    (mb / "decisionLog.md").write_text(
        "# Decision Log\n\n"
        "[2024-05-01 10:30:00] - Chose REST over GraphQL\n"
        "[2024-05-02 09:00:00] - Adopted Postgres\n"
    )

    result = runner.invoke(app, ["import", "--memory-bank", str(mb)])
    assert result.exit_code == 0
    assert "0 decision(s) imported" in result.output
    assert "Warning" in result.output
    assert "## Decision:" in result.output  # names the expected format


def test_import_memory_bank_proper_heading_no_warning(
    tmp_path: Path, monkeypatch, full_memory_bank: Path
):
    """A decisionLog that uses the expected heading imports without the warning."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import", "--memory-bank", str(full_memory_bank)])
    assert result.exit_code == 0
    assert "2 decision(s) imported" in result.output
    assert "Warning" not in result.output


def test_import_adr_no_matching_files_warns(tmp_path: Path, monkeypatch):
    """An ADR dir with only non-'<NNN>-title.md' files imports nothing and says why."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    adr_dir = tmp_path / "loose_adrs"
    adr_dir.mkdir()
    (adr_dir / "decision-records.md").write_text("# Notes\n\nSome unstructured notes.\n")

    result = runner.invoke(app, ["import", "--adr", str(adr_dir)])
    assert result.exit_code == 0
    assert "0 ADR(s) imported" in result.output
    assert "Warning" in result.output
    assert "<NNN>-title.md" in result.output


# ===========================================================================
# Strict, alternatives-aware ADR extractor (opt-in; default-False path)
# ===========================================================================


def test_strict_extractor_named_rejections_verbatim_drops_conditional():
    """The `### `-aware extractor on a MADR `## Alternatives Considered` section.

    The 0003 ADR fixture names three `### ` options. Two are genuine
    rejections; the third ("Use the Embedded Engine's Daemon Directly") opens
    with a conditional "This may become a valid storage transport ..." and is an
    option held open, not a named rejection. The strict extractor returns the
    two rejections with their bodies verbatim, drops the conditional, and never
    scrapes `## Consequences`.
    """
    content = (FIXTURES / "adr" / "0003-shared-store-daemon.md").read_text()

    result = _extract_adr_alternatives_strict(content)
    assert result is not None
    alternatives = [entry["alternative"] for entry in result]

    # Two named rejections; the conditional "may become" option is excluded.
    assert alternatives == [
        "Keep CLI-Only Embedded Access",
        "Add a Local File Lock Around CLI Writes",
    ]
    assert "Use the Embedded Engine's Daemon Directly" not in alternatives

    # Reason text is the verbatim subsection body — quoted, never composed.
    first = result[0]
    assert first["reason"].startswith("This is the simplest short-term shape")
    assert "central place for request ordering" in first["reason"]
    second = result[1]
    assert second["reason"].startswith("This is a useful emergency guard")

    # The offset points at the `### ` heading line so a caller can cite file:line.
    assert content[first["offset"] :].startswith("### Keep CLI-Only Embedded Access")

    # No `## Consequences` scraping: nothing from the Consequences list leaks in.
    reasons = " ".join(entry["reason"] for entry in result)
    assert "one serialization and recovery boundary" not in reasons


def test_strict_extractor_no_alternatives_section_yields_none():
    """An ADR with no `## Alternatives`/`## Options Considered` section yields
    None — no fabricated rejection, no `## Consequences` fallback."""
    content = (
        "# Use PostgreSQL\n\n"
        "## Context\n\nWe need ACID.\n\n"
        "## Decision\n\nUse PostgreSQL.\n\n"
        "## Consequences\n\n"
        "- Bad, because it rules out SQLite as an alternative\n"
    )
    assert _extract_adr_alternatives_strict(content) is None


def test_strict_extractor_options_considered_heading_supported():
    """`## Options Considered` is recognized alongside `## Alternatives Considered`."""
    content = (
        "# Choose a queue\n\n"
        "## Decision\n\nUse SQS.\n\n"
        "## Options Considered\n\n"
        "### RabbitMQ\n\n"
        "Heavier ops burden for our load.\n\n"
        "### Kafka\n\n"
        "Overkill for low-volume events.\n"
    )
    result = _extract_adr_alternatives_strict(content)
    assert result is not None
    assert [e["alternative"] for e in result] == ["RabbitMQ", "Kafka"]
    assert result[0]["reason"] == "Heavier ops burden for our load."
    assert result[1]["reason"] == "Overkill for low-volume events."


def test_strict_extractor_excludes_held_open_revisit_after_marker():
    """A held-open option phrased "Revisit after v2 ..." is excluded, not recorded
    as a rejection — matching the adopt skill's advertised "revisit after v2"
    held-open example. Guards the deferred-marker set against fabricating a
    rejection from a deferral the source left open."""
    content = (
        "# Choose a transport\n\n"
        "## Decision\n\nUse SSE now.\n\n"
        "## Alternatives Considered\n\n"
        "### WebSockets\n\n"
        "Needs sticky sessions our proxies do not support.\n\n"
        "### A gRPC streaming layer\n\n"
        "Revisit after v2 ships; the streaming primitives are not in place yet.\n"
    )
    result = _extract_adr_alternatives_strict(content)
    assert result is not None
    alternatives = [e["alternative"] for e in result]
    # The genuine rejection is kept; the "revisit after v2" option is held open.
    assert alternatives == ["WebSockets"]
    assert "A gRPC streaming layer" not in alternatives


def test_strict_extractor_keeps_rejection_that_narrates_revisiting():
    """A genuine rejection whose body recounts that the option was revisited and
    reconsidered before being ruled out is KEPT. The deferred markers match
    forward-looking held-open phrasings ("revisit after"), not past-tense
    rejection narration ("we revisited X and rejected it")."""
    content = (
        "# Choose a store\n\n"
        "## Decision\n\nUse the daemon.\n\n"
        "## Alternatives Considered\n\n"
        "### A shared connection pool\n\n"
        "We revisited and reconsidered a pool of embedded handles, then rejected "
        "it: it multiplies store owners instead of defining one.\n"
    )
    result = _extract_adr_alternatives_strict(content)
    assert result is not None
    assert [e["alternative"] for e in result] == ["A shared connection pool"]


def test_strict_extractor_import_omits_rejected_when_none_named(store: Path, tmp_path: Path):
    """On the strict path, an ADR with no alternatives section imports a
    decision with no rejected list — no placeholder reason."""
    adr_dir = tmp_path / "strict_adrs"
    adr_dir.mkdir()
    (adr_dir / "0001-no-alternatives.md").write_text(
        "# Use a thing\n\n## Context\n\nC.\n\n## Decision\n\nDo it.\n\n"
        "## Consequences\n\n- Good, it is simple\n"
    )

    counts = _import_adrs(adr_dir, store, strict_alternatives=True)
    assert counts["imported"] == 1

    decisions = sorted((store / "decisions").glob("*.md"))
    imported = decisions[1].read_text()
    # No fabricated placeholder reason leaks onto the strict path.
    assert "Rejected reason not available in source ADR." not in imported
    assert "## Rejected Alternatives" not in imported


def test_strict_extractor_import_records_named_rejections(store: Path):
    """The strict path imports the fixture's two named rejections with
    their verbatim reasons and skips the conditional option."""
    adr_dir = FIXTURES / "adr"

    counts = _import_adrs(adr_dir, store, strict_alternatives=True)
    assert counts["imported"] == 1

    decisions = sorted((store / "decisions").glob("*.md"))
    imported = decisions[1].read_text()
    assert "## Rejected Alternatives" in imported
    assert "### Keep CLI-Only Embedded Access" in imported
    assert "### Add a Local File Lock Around CLI Writes" in imported
    assert "### Use the Embedded Engine's Daemon Directly" not in imported
    assert "This is the simplest short-term shape" in imported
