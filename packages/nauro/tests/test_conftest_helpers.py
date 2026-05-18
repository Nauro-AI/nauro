"""Tests for the shared telemetry-test helpers exposed via conftest.

These helpers were extracted to remove drift across the telemetry test files
(test_cli_command_invoked, test_mcp_tool_called, test_telemetry_subcommand,
test_telemetry_module, test_project_created_event, test_check_command). Pin
the shape here so the next agent who refactors the helpers cannot drop a
piece without a visible test failure.
"""

from __future__ import annotations

import json

from tests.conftest import TEST_ANONYMOUS_ID, FakeClient, seed_consented_config


def test_test_anonymous_id_is_the_uuid4_used_across_telemetry_tests():
    """The magic UUID that previously appeared in five test files verbatim."""
    assert TEST_ANONYMOUS_ID == "11111111-1111-4111-8111-111111111111"


def test_fake_client_records_event_distinct_id_and_properties():
    fake = FakeClient()

    fake.capture("cli.command_invoked", "user-1", {"command": "init", "success": True})
    fake.capture("project.created", "user-1", {"schema_version": 2})

    assert len(fake.events) == 2
    assert fake.events[0] == {
        "event": "cli.command_invoked",
        "distinct_id": "user-1",
        "properties": {"command": "init", "success": True},
    }
    assert fake.events[1]["event"] == "project.created"


def test_seed_consented_config_writes_expected_shape(tmp_path):
    aid = seed_consented_config(tmp_path, enabled=True)

    assert aid == TEST_ANONYMOUS_ID
    data = json.loads((tmp_path / "config.json").read_text())
    assert data["telemetry"]["anonymous_id"] == TEST_ANONYMOUS_ID
    assert data["telemetry"]["enabled"] is True
    assert data["telemetry"]["consent_version"] == 1
    assert data["telemetry"]["consented_at"] == "2026-04-30T00:00:00Z"


def test_seed_consented_config_disabled_flag_persists(tmp_path):
    seed_consented_config(tmp_path, enabled=False)

    data = json.loads((tmp_path / "config.json").read_text())
    assert data["telemetry"]["enabled"] is False


def test_fake_posthog_fixture_swaps_module_singleton_and_resets(fake_posthog):
    """The fixture must assign FakeClient to nauro.telemetry.client._client
    for the duration of the test and reset to None after.
    """
    import nauro.telemetry.client as client_mod

    assert client_mod._client is fake_posthog
    assert isinstance(fake_posthog, FakeClient)
    assert fake_posthog.events == []


def test_telemetry_key_fixture_sets_env_var(telemetry_key, monkeypatch):
    import os

    assert os.environ.get("NAURO_POSTHOG_KEY") == "phc_test_key_for_unit_tests"
