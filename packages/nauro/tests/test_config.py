"""Tests for nauro config."""

import json
import os

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.config import (
    apply_config_to_env,
    get_config,
    load_config,
    save_config,
    set_config,
    unset_config,
)


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))


runner = CliRunner()


# --- Store layer ---


def test_load_config_empty(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    assert load_config() == {}


def test_save_and_load_config(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    save_config({"api_key": "sk-test-123"})
    assert load_config() == {"api_key": "sk-test-123"}


def test_set_and_get_config(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    set_config("api_key", "sk-test-456")
    assert get_config("api_key") == "sk-test-456"


def test_get_config_missing(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    assert get_config("nonexistent") is None


def test_set_config_overwrites(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    set_config("api_key", "old")
    set_config("api_key", "new")
    assert get_config("api_key") == "new"


def test_unset_config(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    set_config("api_key", "sk-test")
    assert unset_config("api_key") is True
    assert get_config("api_key") is None


def test_unset_config_missing(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    assert unset_config("nonexistent") is False


def test_set_preserves_other_keys(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    set_config("api_key", "sk-test")
    set_config("model", "haiku")
    assert get_config("api_key") == "sk-test"
    assert get_config("model") == "haiku"


def test_apply_config_to_env(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    set_config("api_key", "sk-from-config")
    apply_config_to_env()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-config"


def test_apply_config_does_not_override_env(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")
    set_config("api_key", "sk-from-config")
    apply_config_to_env()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-shell"


def test_apply_config_noop_when_empty(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    apply_config_to_env()
    assert "ANTHROPIC_API_KEY" not in os.environ


# --- CLI ---


def test_config_set_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    result = runner.invoke(app, ["config", "set", "api_key", "sk-test-789"])
    assert result.exit_code == 0
    assert "Saved api_key" in result.output
    assert "sk-t" in result.output  # masked prefix
    assert "Config:" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    # Verify it was persisted
    config_path = tmp_path / "nauro_home" / "config.json"
    data = json.loads(config_path.read_text())
    assert data["api_key"] == "sk-test-789"


def test_config_get_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    set_config("api_key", "sk-ant-abcd1234efgh5678")
    result = runner.invoke(app, ["config", "get", "api_key"])
    assert result.exit_code == 0
    # Should be masked
    assert "sk-a" in result.output
    assert "5678" in result.output
    assert "sk-ant-abcd1234efgh5678" not in result.output


def test_config_get_missing_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    result = runner.invoke(app, ["config", "get", "nonexistent"])
    assert result.exit_code == 1
    assert "(not set)" in result.output


def test_config_list_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    set_config("api_key", "sk-ant-abcd1234efgh5678")
    set_config("model", "haiku")
    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == 0
    assert "api_key:" in result.output
    assert "model: haiku" in result.output


def test_config_list_empty_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == 0
    assert "No configuration set" in result.output


def test_config_unset_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    set_config("api_key", "sk-test")
    result = runner.invoke(app, ["config", "unset", "api_key"])
    assert result.exit_code == 0
    assert "Removed api_key" in result.output
    assert "Config:" in result.output
    assert get_config("api_key") is None


def test_config_unset_missing_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    result = runner.invoke(app, ["config", "unset", "nonexistent"])
    assert result.exit_code == 1
    assert "(not set)" in result.output
