"""Tests for nauro validate command."""

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _setup_project(tmp_path, monkeypatch):
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(tmp_path)
    return store


def test_validate_status_on_non_empty_store(tmp_path, monkeypatch):
    """`validate status` must not crash when the store holds decisions.

    The scaffold seeds 001-initial-setup.md, so the store is non-empty.
    Regression: status used dict access (``d.get(...)``) on the Pydantic
    ``Decision`` objects returned by ``_list_decisions``, raising
    AttributeError on any non-empty store (empty stores passed by accident).
    """
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["validate", "status"])

    assert result.exit_code == 0
    assert "Project: testproj" in result.output
    assert "Total decisions: 1" in result.output
    assert "Active decisions: 1" in result.output


def test_validate_status_empty_store(tmp_path, monkeypatch):
    """An empty decisions set reports zero counts without error."""
    store = register_project("emptyproj", [tmp_path])
    scaffold_project_store("emptyproj", store)
    # Remove the seed decision so the store has no decisions.
    for decision in (store / "decisions").glob("*.md"):
        decision.unlink()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate", "status"])

    assert result.exit_code == 0
    assert "Total decisions: 0" in result.output
    assert "Active decisions: 0" in result.output
