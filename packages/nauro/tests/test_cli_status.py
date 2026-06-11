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


def test_status_shows_store_path(tmp_path, monkeypatch):
    """`nauro status` surfaces the absolute store path.

    The store lives at ~/.nauro/projects/<id>/ — outside any repo — and no other
    command prints it. An agent following the nauro-context skill needs it to
    resolve where to write context/<slug>.md.
    """
    store = _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Store:" in result.output
    assert str(store) in result.output


def test_status_sync_inactive(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Sync          inactive" in result.output


def test_status_no_project_shows_friendly_message(tmp_path, monkeypatch):
    """No resolvable project surfaces the status-specific guidance with exit 1.

    ``resolve_target_project`` raises ``typer.Exit``, which is not a
    ``SystemExit`` subclass — the friendly message only reaches the user when
    the handler catches the right exception type.
    """
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "No project found. Run 'nauro init <name>' to get started." in result.output
