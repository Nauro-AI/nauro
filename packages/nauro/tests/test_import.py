"""Tests for nauro import --memory-bank and --adr."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.commands.import_cmd import _import_adrs, _import_memory_bank
from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.store.snapshot import list_snapshots
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


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

    # state.md should have imported context and progress items
    state_content = (store / "state.md").read_text()
    assert "## Imported from Memory Bank" in state_content
    assert "auth module" in state_content

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

    # state.md should NOT have import header (no activeContext.md)
    state_content = (store / "state.md").read_text()
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
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import", "--memory-bank", "/nonexistent/path"])
    assert result.exit_code == 1
    assert "not a directory" in result.output


def test_import_directory_without_project_brief_cli(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    empty_dir = tmp_path / "empty_context"
    empty_dir.mkdir()

    result = runner.invoke(app, ["import", "--memory-bank", str(empty_dir)])
    assert result.exit_code == 1
    assert "projectBrief.md" in result.output


# --- CLI integration ---


def test_import_cli_full(tmp_path: Path, monkeypatch, full_memory_bank: Path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
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
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import", "--memory-bank", str(partial_memory_bank)])
    assert result.exit_code == 0
    assert "1 file(s) merged" in result.output
    assert "0 decision(s) imported" in result.output


def test_import_cli_no_flags(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["import"])
    assert result.exit_code == 1
    assert "--memory-bank" in result.output


def test_import_cli_with_project_flag(tmp_path: Path, monkeypatch, full_memory_bank: Path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    register_project("proj_a", [tmp_path / "a"])
    store_a = tmp_path / "projects" / "proj_a"
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
    assert "**Confidence:** high" in micro_content
    # Title should have number prefix stripped
    assert "Use microservices architecture" in micro_content

    # Second ADR has "Proposed" status → medium confidence
    grpc_content = decisions[2].read_text()
    assert "**Confidence:** medium" in grpc_content

    # Nygard consequences with "rejected" keyword should be extracted
    assert "monolith" in micro_content


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
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
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
