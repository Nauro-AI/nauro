"""Tests for nauro status command."""

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


def test_status_shows_active_capabilities(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           active" in result.output
    assert "AGENTS.md     active" in result.output
    assert "Decisions:" in result.output


def test_status_sync_inactive(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Sync          inactive" in result.output
