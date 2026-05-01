"""Tests for nauro.store.config.get_telemetry_config."""

import json
import uuid

import pytest


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    """Set up a temporary NAURO_HOME."""
    home = tmp_path / ".nauro"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    return home


def test_first_call_generates_anonymous_id(nauro_home):
    from nauro.store.config import get_telemetry_config

    cfg = get_telemetry_config()
    assert cfg.anonymous_id

    on_disk = json.loads((nauro_home / "config.json").read_text())
    assert on_disk["telemetry"]["anonymous_id"] == cfg.anonymous_id


def test_anonymous_id_is_idempotent(nauro_home):
    from nauro.store.config import get_telemetry_config

    first = get_telemetry_config()
    second = get_telemetry_config()
    assert first.anonymous_id == second.anonymous_id


def test_fresh_config_has_null_consent_fields(nauro_home):
    from nauro.store.config import get_telemetry_config

    cfg = get_telemetry_config()
    assert cfg.enabled is None
    assert cfg.consent_version is None
    assert cfg.consented_at is None


def test_env_override_forces_disabled(nauro_home, monkeypatch):
    (nauro_home / "config.json").write_text(
        json.dumps(
            {
                "telemetry": {
                    "anonymous_id": "11111111-1111-4111-8111-111111111111",
                    "enabled": True,
                    "consent_version": 1,
                    "consented_at": "2026-04-30T00:00:00Z",
                }
            }
        )
    )
    monkeypatch.setenv("NAURO_TELEMETRY", "0")

    from nauro.store.config import get_telemetry_config

    cfg = get_telemetry_config()
    assert cfg.enabled is False


def test_env_override_does_not_mutate_file(nauro_home, monkeypatch):
    initial = {
        "telemetry": {
            "anonymous_id": "11111111-1111-4111-8111-111111111111",
            "enabled": True,
            "consent_version": 1,
            "consented_at": "2026-04-30T00:00:00Z",
        }
    }
    (nauro_home / "config.json").write_text(json.dumps(initial))
    monkeypatch.setenv("NAURO_TELEMETRY", "0")

    from nauro.store.config import get_telemetry_config

    get_telemetry_config()

    on_disk = json.loads((nauro_home / "config.json").read_text())
    assert on_disk["telemetry"]["enabled"] is True


def test_config_file_is_owner_only(nauro_home):
    from nauro.store.config import get_telemetry_config

    get_telemetry_config()

    mode = (nauro_home / "config.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_anonymous_id_is_valid_uuid4(nauro_home):
    from nauro.store.config import get_telemetry_config

    cfg = get_telemetry_config()
    parsed = uuid.UUID(cfg.anonymous_id)
    assert parsed.version == 4
