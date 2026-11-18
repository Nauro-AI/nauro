"""Registry schema errors render as clean messages, not raw tracebacks.

A registry written by an incompatible nauro raises ``RegistrySchemaError`` on
the projects commands and on init. Each renders its existing migration or
upgrade hint as a one-line ``Error:`` and exits 1 without touching the file.
"""

import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app

runner = CliRunner()

_REGISTRY_ERROR_CASES = [
    (1, "manual migration"),
    (3, "Upgrade nauro to a version that supports this schema."),
]

_COMMANDS = [
    (["projects"], "projects"),
    (["projects", "list"], "projects-list"),
    (["projects", "rm", "01TESTPROJECT00000000000", "--yes"], "projects-rm"),
    (["init", "foo"], "init"),
]


@pytest.mark.parametrize("version,expected", _REGISTRY_ERROR_CASES)
@pytest.mark.parametrize("args,command_id", _COMMANDS, ids=[c[1] for c in _COMMANDS])
def test_registry_schema_error_is_clean(
    nauro_home, monkeypatch, tmp_path, version, expected, args, command_id
):
    unlinked = tmp_path / "unlinked"
    unlinked.mkdir(exist_ok=True)
    monkeypatch.chdir(unlinked)
    registry = nauro_home / "registry.json"
    registry.write_text(json.dumps({"projects": {}, "schema_version": version}) + "\n")
    before = registry.read_bytes()

    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert "Error:" in result.output
    assert expected in result.output
    assert "Traceback" not in result.output
    assert "RegistrySchemaError" not in result.output
    assert registry.read_bytes() == before
