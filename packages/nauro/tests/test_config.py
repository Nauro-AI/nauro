"""Tests for nauro config."""

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.config import (
    get_config,
    load_config,
    save_config,
    set_config,
    unset_config,
)

runner = CliRunner()


# --- Store layer ---


def test_load_config_empty(tmp_path, monkeypatch):
    assert load_config() == {}


def test_save_and_load_config(tmp_path, monkeypatch):
    save_config({"secret_key": "sk-test-123"})
    assert load_config() == {"secret_key": "sk-test-123"}


def test_set_and_get_config(tmp_path, monkeypatch):
    set_config("secret_key", "sk-test-456")
    assert get_config("secret_key") == "sk-test-456"


def test_get_config_missing(tmp_path, monkeypatch):
    assert get_config("nonexistent") is None


def test_set_config_overwrites(tmp_path, monkeypatch):
    set_config("secret_key", "old")
    set_config("secret_key", "new")
    assert get_config("secret_key") == "new"


def test_unset_config(tmp_path, monkeypatch):
    set_config("secret_key", "sk-test")
    assert unset_config("secret_key") is True
    assert get_config("secret_key") is None


def test_unset_config_missing(tmp_path, monkeypatch):
    assert unset_config("nonexistent") is False


def test_set_preserves_other_keys(tmp_path, monkeypatch):
    set_config("secret_key", "sk-test")
    set_config("model", "haiku")
    assert get_config("secret_key") == "sk-test"
    assert get_config("model") == "haiku"


# --- CLI ---


def test_config_get_cli(tmp_path, monkeypatch):
    set_config("secret_key", "sk-ant-abcd1234efgh5678")
    result = runner.invoke(app, ["config", "get", "secret_key"])
    assert result.exit_code == 0
    assert "sk-a" in result.output
    assert "5678" in result.output
    assert "sk-ant-abcd1234efgh5678" not in result.output


def test_config_get_missing_cli(tmp_path, monkeypatch):
    result = runner.invoke(app, ["config", "get", "nonexistent"])
    assert result.exit_code == 1
    assert "(not set)" in result.output


def test_config_list_cli(tmp_path, monkeypatch):
    set_config("secret_key", "sk-ant-abcd1234efgh5678")
    set_config("model", "haiku")
    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == 0
    assert "secret_key:" in result.output
    assert "model: haiku" in result.output


def test_config_list_empty_cli(tmp_path, monkeypatch):
    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == 0
    assert "No configuration set" in result.output


def test_config_unset_cli(tmp_path, monkeypatch):
    set_config("secret_key", "sk-test")
    result = runner.invoke(app, ["config", "unset", "secret_key"])
    assert result.exit_code == 0
    assert "Removed secret_key" in result.output
    assert "Config:" in result.output
    assert get_config("secret_key") is None


def test_config_unset_missing_cli(tmp_path, monkeypatch):
    result = runner.invoke(app, ["config", "unset", "nonexistent"])
    assert result.exit_code == 1
    assert "(not set)" in result.output
