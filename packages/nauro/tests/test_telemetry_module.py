"""Tests for nauro.telemetry public API and client primitives."""

from __future__ import annotations

import importlib
import logging
import socket
import sys
from unittest.mock import patch

import pytest

from tests.conftest import seed_consented_config


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / ".nauro"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """Each test gets a fresh client singleton — protects against leak across tests."""
    import nauro.telemetry.client as client_mod

    client_mod._client = None
    yield
    client_mod._client = None


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
    seed_consented_config(nauro_home, enabled=False)

    from nauro.telemetry import _should_emit

    assert _should_emit() is False


def test_should_emit_false_when_no_key_resolves(nauro_home, monkeypatch):
    """Consent on + no resolvable key => no emit.

    Pins _BAKED_PROJECT_KEY to empty so this asserts the key gate itself,
    independent of the real key now baked into the shipped wheel. The
    placeholder-prefix branch of the same gate is covered separately.
    """
    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.setattr("nauro.telemetry.client._BAKED_PROJECT_KEY", "")
    seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _should_emit

    assert _should_emit() is False


def test_should_emit_true_when_all_conditions_hold(nauro_home, telemetry_key, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _should_emit

    assert _should_emit() is True


def test_should_emit_false_when_env_var_disables(nauro_home, telemetry_key, monkeypatch):
    monkeypatch.setenv("NAURO_TELEMETRY", "0")
    seed_consented_config(nauro_home, enabled=True)

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
    aid = seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _get_distinct_id

    assert _get_distinct_id() == aid


# --- Baked-in project key resolution -----------------------------------------
# The published wheel ships a baked-in PostHog ingestion key. Until the real
# key is substituted at release time, the constant is the self-disabling
# "phc_REPLACE..." placeholder, which MUST resolve to None so CI/dev stay off.

_REALISTIC_BAKED_KEY = "phc_realistic_baked_project_key_value"


def test_resolve_placeholder_baked_key_returns_none(monkeypatch):
    """The shipped placeholder keeps telemetry off — resolution yields None."""
    import nauro.telemetry.client as client_mod

    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", "phc_REPLACE_WITH_PROD_KEY")

    assert client_mod._resolve_project_key() is None


def test_resolve_empty_baked_key_returns_none(monkeypatch):
    """A falsy baked key (defensive) also resolves to None."""
    import nauro.telemetry.client as client_mod

    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", "")

    assert client_mod._resolve_project_key() is None


def test_resolve_realistic_baked_key_is_used(monkeypatch):
    """Once the real key is baked in, resolution returns it with no env var set."""
    import nauro.telemetry.client as client_mod

    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", _REALISTIC_BAKED_KEY)

    assert client_mod._resolve_project_key() == _REALISTIC_BAKED_KEY


def test_env_var_overrides_realistic_baked_key(monkeypatch):
    """NAURO_POSTHOG_KEY wins over the baked-in key when both are set."""
    import nauro.telemetry.client as client_mod

    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_env_override_key")
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", _REALISTIC_BAKED_KEY)

    assert client_mod._resolve_project_key() == "phc_env_override_key"


def test_env_var_overrides_placeholder_baked_key(monkeypatch):
    """The env var still takes precedence even while the placeholder ships."""
    import nauro.telemetry.client as client_mod

    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_env_override_key")
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", "phc_REPLACE_WITH_PROD_KEY")

    assert client_mod._resolve_project_key() == "phc_env_override_key"


def test_get_client_non_none_with_realistic_baked_key(nauro_home, monkeypatch):
    """A realistic baked key (no env var) yields a live client singleton.

    Resolution itself is asserted at the no-network _resolve_project_key()
    level above; here we only need a placeholder-free key so get_client()
    reaches its construction branch. We monkeypatch the Posthog constructor to
    a sentinel so the test neither makes a network call nor leaks a live
    client (with its background threads) into the shared _client singleton.
    """
    import nauro.telemetry.client as client_mod

    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", _REALISTIC_BAKED_KEY)

    sentinel = object()
    monkeypatch.setattr("posthog.Posthog", lambda **kwargs: sentinel)

    client = client_mod.get_client()
    assert client is sentinel


def test_get_client_none_with_placeholder_baked_key(nauro_home, monkeypatch):
    """The shipped placeholder keeps get_client() at None — telemetry stays off."""
    import nauro.telemetry.client as client_mod

    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", "phc_REPLACE_WITH_PROD_KEY")

    assert client_mod.get_client() is None


def test_get_client_silences_posthog_logger(nauro_home, monkeypatch):
    """get_client() pins the "posthog" logger to CRITICAL after construction.

    posthog catches transport errors internally and logs them at ERROR via the
    "posthog" logger without re-raising, so capture()'s try/except can't stop a
    full traceback from reaching stderr when the network is unreachable (offline,
    or a sandboxed agent). The override must land AFTER Posthog(), which itself
    resets the logger to WARNING — so a fake constructor that mimics that reset
    guards the ordering as well as the final level.
    """
    import nauro.telemetry.client as client_mod

    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", _REALISTIC_BAKED_KEY)

    posthog_logger = logging.getLogger("posthog")
    original_level = posthog_logger.level
    posthog_logger.setLevel(logging.NOTSET)

    def _fake_posthog(**kwargs):
        # Mimic Posthog.__init__ pinning its logger to WARNING; the override in
        # get_client() must run after this to win.
        posthog_logger.setLevel(logging.WARNING)
        return object()

    monkeypatch.setattr("posthog.Posthog", _fake_posthog)

    try:
        client_mod.get_client()
        assert posthog_logger.level == logging.CRITICAL
    finally:
        # Restore the process-global logger level so this test doesn't leak state.
        posthog_logger.setLevel(original_level)


def test_should_emit_false_under_shipped_placeholder(nauro_home, monkeypatch):
    """End-to-end: consented config + shipped placeholder baked key => no emit.

    This is the invariant that lets CI and untouched dev installs ship with the
    constant present yet telemetry fully dark.
    """
    import nauro.telemetry.client as client_mod

    monkeypatch.delenv("NAURO_POSTHOG_KEY", raising=False)
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    monkeypatch.setattr(client_mod, "_BAKED_PROJECT_KEY", "phc_REPLACE_WITH_PROD_KEY")
    seed_consented_config(nauro_home, enabled=True)

    from nauro.telemetry import _should_emit

    assert _should_emit() is False


# identify_login / identify_logout are implemented now.
# Comprehensive coverage lives in tests/test_identity_lifecycle.py.
