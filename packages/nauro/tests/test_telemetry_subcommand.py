"""Tests for nauro telemetry subcommand."""

from __future__ import annotations

import json
import re
import uuid

import pytest
from typer.testing import CliRunner

from tests.conftest import TEST_ANONYMOUS_ID, make_nauro_home, seed_consented_config

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    return make_nauro_home(tmp_path, monkeypatch, dirname="user_home", chdir_repo=True)


def _seed_config(home, *, enabled, consent_version, consented_at, anonymous_id) -> None:
    seed_consented_config(
        home,
        enabled=enabled,
        consent_version=consent_version,
        consented_at=consented_at,
        anonymous_id=anonymous_id,
    )


def test_status_fresh_config(nauro_home):
    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["telemetry", "status"])
    assert result.exit_code == 0, result.output

    out = result.stdout
    assert "enabled (config):    null" in out
    assert "consented_at:        not yet recorded" in out
    # anonymous_id was generated on first read
    line = next(line for line in out.splitlines() if line.startswith("anonymous_id:"))
    aid = line.split(":", 1)[1].strip()
    assert _UUID4_RE.match(aid), f"expected UUID4, got {aid!r}"


def test_disable_persists_and_blocks_emit(nauro_home, telemetry_key):
    _seed_config(
        nauro_home,
        enabled=True,
        consent_version=1,
        consented_at="2026-04-30T00:00:00Z",
        anonymous_id=TEST_ANONYMOUS_ID,
    )

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["telemetry", "disable"])
    assert result.exit_code == 0, result.output
    assert "Telemetry disabled." in result.stdout

    data = json.loads((nauro_home / "config.json").read_text())
    assert data["telemetry"]["enabled"] is False
    assert data["telemetry"]["consented_at"] is not None

    from nauro.telemetry import _should_emit

    assert _should_emit() is False


def test_enable_persists_and_allows_emit(nauro_home, telemetry_key, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed_config(
        nauro_home,
        enabled=False,
        consent_version=1,
        consented_at="2026-04-30T00:00:00Z",
        anonymous_id=TEST_ANONYMOUS_ID,
    )

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["telemetry", "enable"])
    assert result.exit_code == 0, result.output
    assert "Telemetry enabled." in result.stdout

    data = json.loads((nauro_home / "config.json").read_text())
    assert data["telemetry"]["enabled"] is True
    assert data["telemetry"]["consent_version"] == 1
    assert data["telemetry"]["consented_at"] is not None

    from nauro.telemetry import _should_emit

    assert _should_emit() is True


def test_reset_rotates_anonymous_id_preserves_consent(nauro_home):
    original_aid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _seed_config(
        nauro_home,
        enabled=True,
        consent_version=1,
        consented_at="2026-04-30T00:00:00Z",
        anonymous_id=original_aid,
    )

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["telemetry", "reset"])
    assert result.exit_code == 0, result.output
    assert "Rotated anonymous_id." in result.stdout
    assert "Consent state preserved" in result.stdout

    data = json.loads((nauro_home / "config.json").read_text())
    new_aid = data["telemetry"]["anonymous_id"]
    assert new_aid != original_aid
    assert _UUID4_RE.match(new_aid)
    # Consent fields untouched
    assert data["telemetry"]["enabled"] is True
    assert data["telemetry"]["consent_version"] == 1
    assert data["telemetry"]["consented_at"] == "2026-04-30T00:00:00Z"


def test_status_shows_env_override_active(nauro_home, monkeypatch):
    monkeypatch.setenv("NAURO_TELEMETRY", "0")

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["telemetry", "status"])
    assert result.exit_code == 0, result.output

    out = result.stdout
    assert "NAURO_TELEMETRY override:" in out
    assert "'0'" in out
    assert "disables telemetry" in out
    assert "enabled (effective): false" in out


def test_rotate_anonymous_id_helper_unit(nauro_home):
    _seed_config(
        nauro_home,
        enabled=True,
        consent_version=1,
        consented_at="2026-04-30T00:00:00Z",
        anonymous_id=TEST_ANONYMOUS_ID,
    )

    from nauro.telemetry import _rotate_anonymous_id

    new_id_1 = _rotate_anonymous_id()
    new_id_2 = _rotate_anonymous_id()
    assert uuid.UUID(new_id_1).version == 4
    assert uuid.UUID(new_id_2).version == 4
    assert new_id_1 != new_id_2

    data = json.loads((nauro_home / "config.json").read_text())
    assert data["telemetry"]["anonymous_id"] == new_id_2
    assert data["telemetry"]["enabled"] is True
    assert data["telemetry"]["consent_version"] == 1
    assert data["telemetry"]["consented_at"] == "2026-04-30T00:00:00Z"


def test_telemetry_no_args_shows_help(nauro_home):
    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["telemetry"])
    # Typer raises Exit(2) for no_args_is_help on subcommand groups, but newer
    # Typer versions return 0; just assert help text is present in either case.
    assert "status" in result.stdout
    assert "enable" in result.stdout
    assert "disable" in result.stdout
    assert "reset" in result.stdout
