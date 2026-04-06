"""Tests for nauro.sync.config."""

import json

import pytest


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    """Set up a temporary NAURO_HOME."""
    home = tmp_path / ".nauro"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    return home


class TestLoadSyncConfig:
    def test_no_config_file(self, nauro_home):
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.enabled is False
        assert config.bucket_name == ""

    def test_no_sync_key(self, nauro_home):
        (nauro_home / "config.json").write_text(json.dumps({"api_key": "sk-test"}))
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.enabled is False

    def test_empty_credentials(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps({"sync": {"bucket_name": "", "access_key_id": "", "secret_access_key": ""}})
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.enabled is False

    def test_valid_config(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "my-bucket",
                        "region": "us-east-1",
                        "access_key_id": "AKID",
                        "secret_access_key": "secret",
                        "sync_interval": 60,
                    }
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.enabled is True
        assert config.bucket_name == "my-bucket"
        assert config.region == "us-east-1"
        assert config.access_key_id == "AKID"
        assert config.secret_access_key == "secret"
        assert config.sync_interval == 60

    def test_env_var_overrides(self, nauro_home, monkeypatch):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "file-bucket",
                        "access_key_id": "file-key",
                        "secret_access_key": "file-secret",
                    }
                }
            )
        )
        monkeypatch.setenv("NAURO_SYNC_BUCKET_NAME", "env-bucket")
        monkeypatch.setenv("NAURO_SYNC_ACCESS_KEY_ID", "env-key")
        monkeypatch.setenv("NAURO_SYNC_SECRET_ACCESS_KEY", "env-secret")
        monkeypatch.setenv("NAURO_SYNC_REGION", "ap-southeast-1")

        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.enabled is True
        assert config.bucket_name == "env-bucket"
        assert config.region == "ap-southeast-1"
        assert config.access_key_id == "env-key"
        assert config.secret_access_key == "env-secret"

    def test_default_region(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "b",
                        "access_key_id": "k",
                        "secret_access_key": "s",
                    }
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.region == "eu-north-1"

    def test_default_sync_interval(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "b",
                        "access_key_id": "k",
                        "secret_access_key": "s",
                    }
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.sync_interval == 30

    def test_sanitized_sub_from_auth_key(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "b",
                        "access_key_id": "k",
                        "secret_access_key": "s",
                    },
                    "auth": {
                        "sanitized_sub": "auth0-abc123",
                    },
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.sanitized_sub == "auth0-abc123"

    def test_user_id_from_auth_key(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "b",
                        "access_key_id": "k",
                        "secret_access_key": "s",
                    },
                    "auth": {
                        "sanitized_sub": "auth0-abc123",
                        "user_id": "01JQXYZ",
                    },
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.user_id == "01JQXYZ"
        assert config.sanitized_sub == "auth0-abc123"

    def test_missing_user_id_gives_empty(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "b",
                        "access_key_id": "k",
                        "secret_access_key": "s",
                    },
                    "auth": {
                        "sanitized_sub": "auth0-abc123",
                    },
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.user_id == ""

    def test_missing_auth_key_gives_empty_sub(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "b",
                        "access_key_id": "k",
                        "secret_access_key": "s",
                    }
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.sanitized_sub == ""

    def test_auth_key_without_sanitized_sub(self, nauro_home):
        (nauro_home / "config.json").write_text(
            json.dumps(
                {
                    "sync": {
                        "bucket_name": "b",
                        "access_key_id": "k",
                        "secret_access_key": "s",
                    },
                    "auth": {"some_other_key": "value"},
                }
            )
        )
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        assert config.sanitized_sub == ""


class TestS3Prefix:
    def test_builds_correct_prefix(self):
        from nauro.sync.config import s3_prefix

        assert s3_prefix("auth0-abc123", "myproject") == "users/auth0-abc123/projects/myproject/"

    def test_uses_user_id(self):
        from nauro.sync.config import s3_prefix

        assert s3_prefix("01JQXYZ", "myproject") == "users/01JQXYZ/projects/myproject/"

    def test_different_sub_different_prefix(self):
        from nauro.sync.config import s3_prefix

        p1 = s3_prefix("user-a", "proj")
        p2 = s3_prefix("user-b", "proj")
        assert p1 != p2
        assert p1 == "users/user-a/projects/proj/"
        assert p2 == "users/user-b/projects/proj/"


class TestRequireAuth:
    def test_prefers_user_id(self):
        from nauro.sync.config import SyncConfig, require_auth

        config = SyncConfig(user_id="01JQXYZ", sanitized_sub="auth0-abc123")
        assert require_auth(config) == "01JQXYZ"

    def test_falls_back_to_sanitized_sub(self):
        from nauro.sync.config import SyncConfig, require_auth

        config = SyncConfig(user_id="", sanitized_sub="auth0-abc123")
        assert require_auth(config) == "auth0-abc123"

    def test_raises_when_neither(self):
        from nauro.sync.config import AuthRequiredError, SyncConfig, require_auth

        config = SyncConfig(user_id="", sanitized_sub="")
        with pytest.raises(AuthRequiredError, match="nauro auth login"):
            require_auth(config)
