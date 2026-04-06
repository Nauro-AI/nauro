"""Tests for nauro status command."""

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _setup_project(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(tmp_path)
    return store


def test_status_with_api_key(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Extraction    active" in result.output
    assert "MCP           active" in result.output
    assert "AGENTS.md     active" in result.output


def test_status_without_api_key(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Extraction    inactive" in result.output
    assert "add API key to enable" in result.output


def test_status_sync_inactive(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Sync          inactive" in result.output
