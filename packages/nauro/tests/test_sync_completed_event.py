"""Tests for the `sync.completed` activation event.

`nauro sync` must emit exactly one `sync.completed` per snapshot capture,
carrying only the PRIVACY.md-documented property set
{ snapshot_count, duration_bucket, bytes_bucket }. Mirrors the
project.created / mcp.tool_called emission tests.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from typer.testing import CliRunner

from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import FakeClient, seed_consented_config

_SYNC_COMPLETED_KEYS = frozenset({"snapshot_count", "duration_bucket", "bytes_bucket"})
_DURATION_PATTERN = re.compile(r"^(<10ms|10-100ms|100ms-1s|1-10s|>10s)$")
_BYTES_PATTERN = re.compile(r"^(<10KB|10-100KB|100KB-1MB|1-10MB|>10MB)$")

runner = CliRunner()


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / "user_home"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    return home


@pytest.fixture
def project_store(nauro_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = register_project("syncproj", [repo])
    scaffold_project_store("syncproj", store)
    monkeypatch.chdir(repo)
    return store


def _events_named(fake: FakeClient, name: str) -> list[dict[str, Any]]:
    return [e for e in fake.events if e["event"] == name]


def test_sync_emits_exactly_one_sync_completed(
    nauro_home, project_store, telemetry_key, fake_posthog
):
    seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output

    events = _events_named(fake_posthog, "sync.completed")
    assert len(events) == 1


def test_sync_completed_property_keys_are_exhaustive(
    nauro_home, project_store, telemetry_key, fake_posthog
):
    seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output

    events = _events_named(fake_posthog, "sync.completed")
    assert len(events) == 1
    props = events[0]["properties"]
    assert set(props.keys()) == _SYNC_COMPLETED_KEYS


def test_sync_completed_property_values_are_well_formed(
    nauro_home, project_store, telemetry_key, fake_posthog
):
    seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output

    props = _events_named(fake_posthog, "sync.completed")[0]["properties"]
    # A scaffolded store captures exactly one snapshot, so the post-capture
    # count is 1.
    assert props["snapshot_count"] == 1
    assert isinstance(props["snapshot_count"], int)
    assert _DURATION_PATTERN.match(props["duration_bucket"]), props["duration_bucket"]
    assert _BYTES_PATTERN.match(props["bytes_bucket"]), props["bytes_bucket"]


def test_sync_completed_not_emitted_when_telemetry_disabled(
    nauro_home, project_store, telemetry_key, fake_posthog
):
    """Consent off => _should_emit() is False => no sync.completed escapes."""
    seed_consented_config(nauro_home, enabled=False)

    from nauro.cli.main import app

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output

    assert _events_named(fake_posthog, "sync.completed") == []


def test_repeated_sync_emits_one_event_each(nauro_home, project_store, telemetry_key, fake_posthog):
    """Snapshot count climbs across runs; each run emits exactly one event."""
    seed_consented_config(nauro_home, enabled=True)

    from nauro.cli.main import app

    for _ in range(3):
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output

    events = _events_named(fake_posthog, "sync.completed")
    assert len(events) == 3
    counts = [e["properties"]["snapshot_count"] for e in events]
    assert counts == [1, 2, 3]
