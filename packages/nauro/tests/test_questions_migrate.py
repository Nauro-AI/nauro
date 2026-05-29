"""Tests for `nauro questions migrate`.

The command mints sequential `Q###` ids for legacy `[timestamp]` entries in
open-questions.md. The migration logic itself lives in
`nauro_core.questions`; these tests pin the CLI surface — store resolution,
the dry-run/apply split, the summary output, and AGENTS.md refresh — against
an isolated store.
"""

from pathlib import Path

from nauro_core.constants import OPEN_QUESTIONS_MD
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

_LEGACY_FILE = (
    "# Open Questions\n"
    "\n"
    "- [2026-05-12 20:18 UTC] first legacy q\n"
    "- [2026-05-11 15:29 UTC] second legacy q\n"
)


def _setup_project(tmp_path: Path, monkeypatch, questions: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = register_project("myproj", [repo])
    scaffold_project_store("myproj", store)
    (store / OPEN_QUESTIONS_MD).write_text(questions)
    monkeypatch.chdir(repo)
    return store


def test_migrate_rewrites_legacy_ids_to_q_form(tmp_path: Path, monkeypatch):
    store = _setup_project(tmp_path, monkeypatch, _LEGACY_FILE)

    result = runner.invoke(app, ["questions", "migrate"])
    assert result.exit_code == 0, result.output

    assert (store / OPEN_QUESTIONS_MD).read_text() == (
        "# Open Questions\n"
        "\n"
        "- [Q1] first legacy q (logged 2026-05-12 20:18 UTC)\n"
        "- [Q2] second legacy q (logged 2026-05-11 15:29 UTC)\n"
    )
    assert "Migrated 2 entry(ies) in myproj" in result.output
    assert "[2026-05-12 20:18 UTC] -> [Q1]" in result.output
    assert "[2026-05-11 15:29 UTC] -> [Q2]" in result.output


def test_migrate_dry_run_writes_nothing(tmp_path: Path, monkeypatch):
    store = _setup_project(tmp_path, monkeypatch, _LEGACY_FILE)

    result = runner.invoke(app, ["questions", "migrate", "--dry-run"])
    assert result.exit_code == 0, result.output

    # File is untouched.
    assert (store / OPEN_QUESTIONS_MD).read_text() == _LEGACY_FILE
    assert "Would migrate 2 entry(ies) in myproj" in result.output
    assert "[2026-05-12 20:18 UTC] -> [Q1]" in result.output
    assert "+(logged 2026-05-12 20:18 UTC)" in result.output
    assert "Dry run: no changes written." in result.output


def test_migrate_noop_when_all_q_form(tmp_path: Path, monkeypatch):
    already_migrated = "# Open Questions\n\n- [Q1] one\n- [Q2] two\n"
    store = _setup_project(tmp_path, monkeypatch, already_migrated)

    result = runner.invoke(app, ["questions", "migrate"])
    assert result.exit_code == 0, result.output

    assert (store / OPEN_QUESTIONS_MD).read_text() == already_migrated
    assert "No legacy question entries to migrate in myproj." in result.output


def test_migrate_refreshes_agents_md(tmp_path: Path, monkeypatch):
    """Open questions surface in AGENTS.md, so the migrated ids must too."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = register_project("myproj", [repo])
    scaffold_project_store("myproj", store)
    (store / OPEN_QUESTIONS_MD).write_text(
        "# Open Questions\n\n- [2026-05-12 20:18 UTC] surfaced question\n"
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["questions", "migrate"])
    assert result.exit_code == 0, result.output

    agents_md = repo / "AGENTS.md"
    assert agents_md.exists()
    content = agents_md.read_text()
    assert "[Q1]" in content
    assert "Updated AGENTS.md" in result.output
    assert str(repo) in result.output


def test_migrate_continues_past_existing_q_ids(tmp_path: Path, monkeypatch):
    mixed = (
        "# Open Questions\n"
        "\n"
        "- [Q5] already migrated\n"
        "- [2026-05-12 20:18 UTC] legacy after a high Q\n"
    )
    store = _setup_project(tmp_path, monkeypatch, mixed)

    result = runner.invoke(app, ["questions", "migrate"])
    assert result.exit_code == 0, result.output

    assert (store / OPEN_QUESTIONS_MD).read_text() == (
        "# Open Questions\n"
        "\n"
        "- [Q5] already migrated\n"
        "- [Q6] legacy after a high Q (logged 2026-05-12 20:18 UTC)\n"
    )
    assert "[2026-05-12 20:18 UTC] -> [Q6]" in result.output


def test_migrate_unknown_project_rejected(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = register_project("myproj", [repo])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["questions", "migrate", "--project", "nope"])
    assert result.exit_code == 1
    assert "Unknown project 'nope'." in result.output
