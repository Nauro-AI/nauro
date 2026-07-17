"""Compatibility coverage for the retired telemetry command group."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tests.conftest import make_nauro_home

_RETIRED_MESSAGE = (
    "Product telemetry has been removed. This deprecated compatibility command "
    "makes no changes and will be removed in Nauro 2.0."
)


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    return make_nauro_home(tmp_path, monkeypatch, dirname="user_home", chdir_repo=True)


@pytest.mark.parametrize("command", ["status", "enable", "disable", "reset"])
def test_telemetry_commands_are_inert_compatibility_shims(nauro_home, command):
    config_path = nauro_home / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "auth": {"access_token": "keep-me"},
                "telemetry": {
                    "anonymous_id": "legacy-id",
                    "enabled": True,
                    "consent_version": 99,
                    "consented_at": "2026-04-30T00:00:00Z",
                    "unknown": {"preserve": [1, "two", None]},
                },
            },
            indent=4,
        )
        + "\n",
        encoding="utf-8",
    )
    before = config_path.read_bytes()

    from nauro.cli.main import app

    result = CliRunner().invoke(app, ["telemetry", command])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == _RETIRED_MESSAGE
    assert config_path.read_bytes() == before
    assert not config_path.with_suffix(".lock").exists()


def test_first_cli_run_does_not_prompt_or_create_telemetry_config(nauro_home):
    from nauro.cli.main import app

    result = CliRunner().invoke(app, ["config", "list"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "No configuration set.\n"
    assert not (nauro_home / "config.json").exists()
    assert not (nauro_home / "config.lock").exists()


def test_telemetry_no_args_keeps_frozen_subcommand_tree(nauro_home):
    from nauro.cli.main import app

    result = CliRunner().invoke(app, ["telemetry"])

    for command in ("status", "enable", "disable", "reset"):
        assert command in result.stdout
