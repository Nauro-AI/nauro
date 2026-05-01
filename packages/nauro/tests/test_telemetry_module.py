"""Tests for nauro.telemetry public API and client primitives."""

from __future__ import annotations

import importlib
import json
import socket
import sys
from unittest.mock import patch

import pytest


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / ".nauro"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    return home


@pytest.fixture
def telemetry_key(monkeypatch):
    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_test_key_for_unit_tests")


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """Each test gets a fresh client singleton — protects against leak across tests."""
    import nauro.telemetry.client as client_mod

    client_mod._client = None
    yield
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


def test_import_does_not_create_socket():
    for name in [
        "nauro.telemetry",
        "nauro.telemetry.client",
        "nauro.telemetry.events",
        "nauro.telemetry.consent",
    ]:
        sys.modules.pop(name, None)

    with patch.object(socket, "socket") as mock_socket:
        importlib.import_module("nauro.telemetry")
        importlib.import_module("nauro.telemetry.client")
        importlib.import_module("nauro.telemetry.events")
        importlib.import_module("nauro.telemetry.consent")

    assert not mock_socket.called, "telemetry import must be lazy — no socket creation"


def test_should_emit_false_when_enabled_is_none(nauro_home, telemetry_key):
    from nauro.telemetry import _should_emit

    assert _should_emit() is False


def test_should_emit_false_when_enabled_is_false(nauro_home, telemetry_key):
    _seed_consented_config(nauro_home, enabled=False)

    from nauro.telemetry import _should_emit

    assert _should_emit() is False


def test_should_emit_false_when_posthog_key_unset(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _should_emit

    assert _should_emit() is False


def test_should_emit_true_when_all_conditions_hold(nauro_home, telemetry_key, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _should_emit

    assert _should_emit() is True


def test_should_emit_false_when_env_var_disables(nauro_home, telemetry_key, monkeypatch):
    monkeypatch.setenv("NAURO_TELEMETRY", "0")
    _seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _should_emit

    assert _should_emit() is False


def test_strip_reserved_filters_geoip_and_ip_and_user_agent():
    from nauro.telemetry.client import _strip_reserved

    props = {
        "$ip": "1.2.3.4",
        "$geoip_country_code": "US",
        "$geoip_subdivision_1_name": "California",
        "$geoip_city_name": "San Francisco",
        "$user_agent": "curl/8",
        "command": "init",
        "success": True,
    }

    filtered = _strip_reserved(props)

    assert "$ip" not in filtered
    assert "$geoip_country_code" not in filtered
    assert "$geoip_subdivision_1_name" not in filtered
    assert "$geoip_city_name" not in filtered
    assert "$user_agent" not in filtered


def test_strip_reserved_preserves_unrelated_keys():
    from nauro.telemetry.client import _strip_reserved

    props = {
        "command": "sync",
        "success": True,
        "duration_bucket": "100-500ms",
        "nauro_version": "0.1.1",
        "os": "darwin",
    }

    filtered = _strip_reserved(props)

    assert filtered == props


def test_get_distinct_id_returns_anonymous_id(nauro_home):
    aid = _seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _get_distinct_id

    assert _get_distinct_id() == aid


# Phase 1c — identify_login / identify_logout are implemented now.
# Comprehensive coverage lives in tests/test_identity_lifecycle.py.
