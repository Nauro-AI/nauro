"""Tests for nauro telemetry subcommand (T1.9)."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from typer.testing import CliRunner

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class FakeClient:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def capture(
        self,
        event: str,
        distinct_id: str,
        properties: dict[str, Any],
    ) -> None:
        self.events.append({"event": event, "distinct_id": distinct_id, "properties": properties})


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / "user_home"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    return home


@pytest.fixture
def telemetry_key(monkeypatch):
    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_test_key_for_unit_tests")


@pytest.fixture
def fake_posthog(monkeypatch):
    import nauro.telemetry.client as client_mod

    fake = FakeClient()
    client_mod._client = fake
    yield fake
    client_mod._client = None


def _seed_config(home, *, enabled, consent_version, consented_at, anonymous_id) -> None:
    (home / "config.json").write_text(
        json.dumps(
            {
                "telemetry": {
                    "anonymous_id": anonymous_id,
                    "enabled": enabled,
                    "consent_version": consent_version,
                    "consented_at": consented_at,
                }
            }
        )
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
        anonymous_id="11111111-1111-4111-8111-111111111111",
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
        anonymous_id="11111111-1111-4111-8111-111111111111",
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
        anonymous_id="11111111-1111-4111-8111-111111111111",
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
