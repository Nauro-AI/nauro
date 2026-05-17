"""`nauro serve` no longer accepts --project — one source of truth is the repo config."""

from __future__ import annotations

from typer.testing import CliRunner

from nauro.cli.main import app

runner = CliRunner()


def test_serve_rejects_project_flag():
    """The --project flag was removed; passing it is a usage error."""
    result = runner.invoke(app, ["serve", "--project", "anything"])
    assert result.exit_code != 0
    output = result.output + (result.stderr or "")
    assert "--project" in output or "No such option" in output or "no such option" in output
