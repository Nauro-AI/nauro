"""Sync configuration — loads cloud sync settings from ~/.nauro/config.json."""

import os
from dataclasses import dataclass

from nauro.store.config import load_config


@dataclass
class SyncConfig:
    bucket_name: str = ""
    region: str = "eu-north-1"
    access_key_id: str = ""
    secret_access_key: str = ""
    sync_interval: int = 30
    enabled: bool = False
    sanitized_sub: str = ""
    user_id: str = ""


class AuthRequiredError(Exception):
    """Raised when a sync operation requires auth but sanitized_sub is missing."""


def s3_prefix(user_key: str, project_name: str) -> str:
    """Build the S3 key prefix for a project.

    user_key is either user_id (preferred) or sanitized_sub (fallback).
    """
    return f"users/{user_key}/projects/{project_name}/"


def require_auth(config: SyncConfig) -> str:
    """Return user_id (preferred) or sanitized_sub, or raise AuthRequiredError."""
    user_key = config.user_id or config.sanitized_sub
    if not user_key:
        raise AuthRequiredError(
            "Cloud sync requires authentication. Run 'nauro auth login' to set up your account."
        )
    return user_key


def load_sync_config() -> SyncConfig:
    """Load sync config from ~/.nauro/config.json under the 'sync' key.

    Environment variables override file values:
      NAURO_SYNC_BUCKET_NAME, NAURO_SYNC_REGION,
      NAURO_SYNC_ACCESS_KEY_ID, NAURO_SYNC_SECRET_ACCESS_KEY

    Auth identity is loaded from the 'auth' key:
      {"auth": {"sanitized_sub": "auth0-abc123"}}
    """
    data = load_config()
    sync_data = data.get("sync", {})
    auth_data = data.get("auth", {})

    bucket_name = os.environ.get("NAURO_SYNC_BUCKET_NAME", sync_data.get("bucket_name", ""))
    region = os.environ.get("NAURO_SYNC_REGION", sync_data.get("region", "eu-north-1"))
    access_key_id = os.environ.get("NAURO_SYNC_ACCESS_KEY_ID", sync_data.get("access_key_id", ""))
    secret_access_key = os.environ.get(
        "NAURO_SYNC_SECRET_ACCESS_KEY", sync_data.get("secret_access_key", "")
    )
    sync_interval = sync_data.get("sync_interval", 30)
    sanitized_sub = auth_data.get("sanitized_sub", "")
    user_id = auth_data.get("user_id", "")

    enabled = bool(bucket_name and access_key_id and secret_access_key)

    return SyncConfig(
        bucket_name=bucket_name,
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        sync_interval=sync_interval,
        enabled=enabled,
        sanitized_sub=sanitized_sub,
        user_id=user_id,
    )
