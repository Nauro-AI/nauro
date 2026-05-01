"""Tests for cli.command_invoked telemetry instrumentation (T1.4)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

_EXPECTED_KEYS = {"command", "success", "duration_bucket", "nauro_version", "os"}
_DISALLOWED_KEYS = {"args", "argv", "cwd", "env", "exit_code"}


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


def _seed_consented_config(home, *, enabled: bool) -> str:
    aid = "11111111-1111-4111-8111-111111111111"
    (home / "config.json").write_text(
        json.dumps(
            {
                "telemetry": {
                    "anonymous_id": aid,
                    "enabled": enabled,
                    "consent_version": 1,
                    "consented_at": "2026-04-30T00:00:00Z",
                }
            }
        )
    )
    return aid


def _command_events(fake: FakeClient) -> list[dict[str, Any]]:
    return [e for e in fake.events if e["event"] == "cli.command_invoked"]


def _build_isolated_app(callback) -> typer.Typer:
    """Build a one-command Typer app with the given callback already instrumented.

    Typer treats a single-command app as the default command, so invoke with [].
    """
    from nauro.telemetry.cli_wrapper import instrument

    app = typer.Typer()
    app.command()(instrument(callback, command_path="run"))
    return app


def test_emits_one_event_on_success(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0, result.output

    events = _command_events(fake_posthog)
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["command"] == "init"
    assert props["success"] is True
    assert props["duration_bucket"] in {"<10ms", "10-100ms", "100ms-1s", "1-10s", ">10s"}
    assert isinstance(props["nauro_version"], str)
    assert isinstance(props["os"], str)


def test_emits_one_event_on_typer_exit_nonzero(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    def _fail() -> None:
        raise typer.Exit(code=1)

    app = _build_isolated_app(_fail)
    runner = CliRunner()
    result = runner.invoke(app, [])
    assert result.exit_code == 1

    events = _command_events(fake_posthog)
    assert len(events) == 1
    assert events[0]["properties"]["command"] == "run"
    assert events[0]["properties"]["success"] is False


def test_emits_one_event_on_typer_exit_zero(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    def _early_exit() -> None:
        raise typer.Exit()  # default code=0

    app = _build_isolated_app(_early_exit)
    runner = CliRunner()
    result = runner.invoke(app, [])
    assert result.exit_code == 0

    events = _command_events(fake_posthog)
    assert len(events) == 1
    assert events[0]["properties"]["success"] is True


def test_emits_one_event_on_unexpected_exception(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    def _boom() -> None:
        raise RuntimeError("boom")

    app = _build_isolated_app(_boom)
    runner = CliRunner()
    result = runner.invoke(app, [])
    assert result.exit_code != 0

    events = _command_events(fake_posthog)
    assert len(events) == 1
    assert events[0]["properties"]["command"] == "run"
    assert events[0]["properties"]["success"] is False


def test_dotted_subcommand_path(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["telemetry", "status"])
    assert result.exit_code == 0, result.output

    events = _command_events(fake_posthog)
    assert len(events) == 1
    assert events[0]["properties"]["command"] == "telemetry.status"
    assert events[0]["properties"]["success"] is True


def test_no_event_when_enabled_false(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=False)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0, result.output

    assert fake_posthog.events == []


def test_no_event_when_env_var_zero(nauro_home, telemetry_key, fake_posthog, monkeypatch):
    monkeypatch.setenv("NAURO_TELEMETRY", "0")
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0, result.output

    assert fake_posthog.events == []


def test_event_keys_exhaustive_no_disallowed(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0

    events = _command_events(fake_posthog)
    assert len(events) == 1
    keys = set(events[0]["properties"].keys())
    assert keys == _EXPECTED_KEYS
    assert keys.isdisjoint(_DISALLOWED_KEYS)


def test_bucket_classification():
    from nauro.telemetry.cli_wrapper import _bucket

    assert _bucket(0.005) == "<10ms"
    assert _bucket(0.05) == "10-100ms"
    assert _bucket(0.5) == "100ms-1s"
    assert _bucket(5.0) == "1-10s"
    assert _bucket(50.0) == ">10s"


def test_version_flag_does_not_emit(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0

    assert _command_events(fake_posthog) == []


def test_help_flag_does_not_emit(nauro_home, telemetry_key, fake_posthog):
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0

    assert _command_events(fake_posthog) == []


def test_instrument_is_idempotent():
    from nauro.telemetry.cli_wrapper import instrument

    def _f() -> None:
        return None

    once = instrument(_f, command_path="once")
    twice = instrument(once, command_path="once")
    assert once is twice
