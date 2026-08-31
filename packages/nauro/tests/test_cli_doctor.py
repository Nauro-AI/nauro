"""Tests for the ``nauro doctor`` command.

Command-and-adapter behavior only: exit code posture, project resolution, and
the rendered report shape. The diagnosis logic itself (which defects fire, the
one-to-many guard, ordering) is pinned in the nauro-core suite and is not
re-asserted here.
"""

from __future__ import annotations

from pathlib import Path

from nauro_core.decision_model import Decision, format_decision
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import write_decision_file

runner = CliRunner()


def _new_store(tmp_path: Path, name: str = "docproj") -> Path:
    """Register and scaffold a project, returning its store path."""
    _pid, store = register_project_v2(name, [tmp_path])
    scaffold_project_store(name, store)
    return store


def _decision_md(
    num: int,
    *,
    title: str | None = None,
    supersedes: str | None = None,
) -> str:
    return format_decision(
        Decision(
            date="2026-03-15",
            confidence="high",
            num=num,
            title=title if title is not None else f"Decision {num}",
            rationale=f"Rationale for decision {num}.",
            supersedes=supersedes,
        )
    )


def test_clean_store_exits_zero(tmp_path: Path) -> None:
    _new_store(tmp_path)
    result = runner.invoke(app, ["doctor", "--project", "docproj"])
    assert result.exit_code == 0
    assert "No integrity defects found." in result.stdout


def test_defective_store_exits_zero_and_renders_defects(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    write_decision_file(store, 10, "broken", "this file does not parse as a decision")
    write_decision_file(store, 11, "dangling", _decision_md(11, supersedes="999"))

    result = runner.invoke(app, ["doctor", "--project", "docproj"])

    assert result.exit_code == 0
    assert "Unparseable decision files" in result.stdout
    assert "010-broken" in result.stdout
    assert "Dangling supersession refs" in result.stdout
    assert "D11" in result.stdout
    assert "D999" in result.stdout


def test_duplicate_number_renders_exact_carrier_paths_and_counts_group(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    write_decision_file(store, 10, "zeta", _decision_md(10, title="Zeta"))
    write_decision_file(store, 10, "alpha", _decision_md(10, title="Alpha"))

    result = runner.invoke(app, ["doctor", "--project", "docproj"])

    assert result.exit_code == 0
    assert "Duplicate decision numbers (1):" in result.stdout
    assert "D10: decisions/010-alpha.md, decisions/010-zeta.md" in result.stdout
    assert "Found 1 defect(s)." in result.stdout


def test_duplicate_active_title_renders_numbered_carriers_and_counts_group(
    tmp_path: Path,
) -> None:
    store = _new_store(tmp_path)
    write_decision_file(store, 30, "zeta", _decision_md(30, title="  Use   FastAPI  "))
    write_decision_file(store, 10, "alpha", _decision_md(10, title="use fastapi"))

    result = runner.invoke(app, ["doctor", "--project", "docproj"])

    assert result.exit_code == 0
    assert "Duplicate active decision titles (1):" in result.stdout
    assert "use fastapi" in result.stdout
    assert "D10 decisions/010-alpha.md, D30 decisions/030-zeta.md" in result.stdout
    assert "Found 1 defect(s)." in result.stdout


def test_unknown_frontmatter_key_renders_advisory_on_clean_store(tmp_path: Path) -> None:
    """An unknown key is advisory: the store is still clean (exit 0, the
    clean message prints) and the advisory section prints alongside it."""
    store = _new_store(tmp_path)
    canonical = _decision_md(12)
    close = canonical.find("\n---\n", len("---\n"))
    with_unknown = canonical[:close] + "\norigin: codex-1.2.3" + canonical[close:]
    write_decision_file(store, 12, "unknown-key", with_unknown)

    result = runner.invoke(app, ["doctor", "--project", "docproj"])

    assert result.exit_code == 0
    assert "No integrity defects found." in result.stdout
    assert "Unknown frontmatter keys" in result.stdout
    assert "D12" in result.stdout
    assert "origin" in result.stdout


def test_supersede_orphan_renders_its_own_section_and_repair_remedy(tmp_path: Path) -> None:
    """An orphan is repairable, not blocking: the store still reads clean, the
    orphan gets its own counted section, and the section names the remedy."""
    store = _new_store(tmp_path)
    write_decision_file(store, 20, "child", _decision_md(20, supersedes="19"))
    write_decision_file(store, 19, "target", _decision_md(19))

    result = runner.invoke(app, ["doctor", "--project", "docproj"])

    assert result.exit_code == 0
    assert "No integrity defects found." in result.stdout
    assert "Supersede backref orphans (1)" in result.stdout
    assert "D20 supersedes D19" in result.stdout
    assert "nauro repair" in result.stdout
    assert "Found 1 repairable defect(s)." in result.stdout


def test_clean_store_report_is_unchanged_by_the_orphan_section(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    write_decision_file(store, 2, "settled", _decision_md(2))

    result = runner.invoke(app, ["doctor", "--project", "docproj"])

    assert result.exit_code == 0
    assert result.stdout == "Project: docproj\n\nNo integrity defects found.\n"


def test_project_resolution_and_report_header(tmp_path: Path) -> None:
    _new_store(tmp_path, name="another")
    result = runner.invoke(app, ["doctor", "--project", "another"])
    assert result.exit_code == 0
    assert "Project: another" in result.stdout


def test_unknown_project_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--project", "nope"])
    assert result.exit_code == 1


def test_help_states_store_only_scope_and_points_at_status() -> None:
    """Doctor's help draws the boundary: store integrity here, everything
    else (connection, wiring) is status's job."""
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "decision store" in result.stdout
    assert "nauro status" in result.stdout
