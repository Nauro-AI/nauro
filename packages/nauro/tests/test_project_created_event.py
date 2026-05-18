"""Tests for project.created funnel-anchor event (T1.6)."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeClient, seed_consented_config


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / "user_home"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    return home


def _events_named(fake: FakeClient, name: str) -> list[dict[str, Any]]:
    return [e for e in fake.events if e["event"] == name]


def test_init_success_emits_project_created_with_schema_version(
    nauro_home, telemetry_key, fake_posthog
):
    seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0, result.output

    project_events = _events_named(fake_posthog, "project.created")
    assert len(project_events) == 1
    assert project_events[0]["properties"] == {"schema_version": 2}

    cli_events = _events_named(fake_posthog, "cli.command_invoked")
    assert len(cli_events) == 1
    assert cli_events[0]["properties"]["command"] == "init"
    assert cli_events[0]["properties"]["success"] is True


def test_add_repo_branch_does_not_emit_project_created(
    nauro_home, telemetry_key, fake_posthog, tmp_path
):
    seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    # First, register the project (this DOES emit project.created)
    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0
    assert len(_events_named(fake_posthog, "project.created")) == 1

    # Reset events and run --add-repo against the existing project
    fake_posthog.events.clear()
    extra_repo = tmp_path / "extra"
    extra_repo.mkdir()
    result = runner.invoke(app, ["init", "testproj", "--add-repo", str(extra_repo)])
    assert result.exit_code == 0, result.output

    assert _events_named(fake_posthog, "project.created") == []
    assert len(_events_named(fake_posthog, "cli.command_invoked")) == 1


def test_init_failure_does_not_emit_project_created(
    nauro_home, telemetry_key, fake_posthog, tmp_path
):
    """--add-repo against a nonexistent multi-match scenario isn't reachable in v2;
    instead simulate failure by making register_project_v2 raise."""
    seed_consented_config(nauro_home, enabled=True)

    import nauro.cli.commands.init as init_module

    def _raise(*args, **kwargs):
        raise ValueError("simulated failure")

    # Patch only for the duration of this test.
    original = init_module.register_project_v2
    init_module.register_project_v2 = _raise  # type: ignore[assignment]
    try:
        from nauro.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["init", "testproj"])
        assert result.exit_code == 1
    finally:
        init_module.register_project_v2 = original  # type: ignore[assignment]

    assert _events_named(fake_posthog, "project.created") == []
    cli_events = _events_named(fake_posthog, "cli.command_invoked")
    assert len(cli_events) == 1
    assert cli_events[0]["properties"]["success"] is False


def test_project_created_keys_are_exhaustive(nauro_home, telemetry_key, fake_posthog):
    seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0

    project_events = _events_named(fake_posthog, "project.created")
    assert len(project_events) == 1
    assert set(project_events[0]["properties"].keys()) == {"schema_version"}
