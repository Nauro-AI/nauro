"""User configuration — manages ~/.nauro/config.json.

Stores user-level settings like API keys. Config values for known keys
(e.g. api_key) are applied to the environment at CLI startup so that
downstream code (Anthropic SDK) picks them up automatically.

Respects NAURO_HOME env var override (defaults to ~/.nauro/).
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from nauro.constants import (
    CONFIG_FILENAME,
    DEFAULT_NAURO_HOME,
    NAURO_HOME_ENV,
    NAURO_TELEMETRY_ENV,
)

logger = logging.getLogger("nauro.config")

# Maps config keys to the environment variable they should populate.
_CONFIG_ENV_MAP = {
    "api_key": "ANTHROPIC_API_KEY",
}


def _config_file() -> Path:
    nauro_home = Path(os.environ.get(NAURO_HOME_ENV, Path.home() / DEFAULT_NAURO_HOME))
    return nauro_home / CONFIG_FILENAME


def load_config() -> dict:
    """Read config.json, return empty dict if it doesn't exist or is corrupt."""
    cf = _config_file()
    if cf.exists():
        try:
            return json.loads(cf.read_text())  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            logger.warning("config.json is corrupt — returning empty config")
            return {}
    return {}


def save_config(data: dict) -> None:
    """Write config.json atomically (write-to-tmp + rename). Restricts to owner-only (0o600)."""
    cf = _config_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    tmp = cf.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, cf)


def get_config(key: str) -> str | None:
    """Get a single config value by key."""
    return load_config().get(key)


def set_config(key: str, value: str) -> None:
    """Set a single config value."""
    data = load_config()
    data[key] = value
    save_config(data)


def unset_config(key: str) -> bool:
    """Remove a config key. Returns True if the key existed."""
    data = load_config()
    if key not in data:
        return False
    del data[key]
    save_config(data)
    return True


def apply_config_to_env() -> None:
    """Load config and set environment variables for known keys.

    Does not override env vars that are already set, so explicit
    exports (e.g. in .bashrc) always take precedence.
    """
    data = load_config()
    for config_key, env_var in _CONFIG_ENV_MAP.items():
        value = data.get(config_key)
        if value and env_var not in os.environ:
            os.environ[env_var] = value


_TELEMETRY_KEY = "telemetry"


@dataclass(frozen=True)
class TelemetryConfig:
    anonymous_id: str
    enabled: bool | None
    consent_version: int | None
    consented_at: str | None


def get_telemetry_config() -> TelemetryConfig:
    """Read telemetry section, generating anonymous_id on first call.

    Applies NAURO_TELEMETRY=0 env override at read time without mutating disk.
    """
    data = load_config()
    section = data.get(_TELEMETRY_KEY) or {}

    anonymous_id = section.get("anonymous_id")
    if not anonymous_id:
        # anonymous_id is generated and persisted before consent so Phase 1
        # can attach the consent record to a stable identity that already exists.
        anonymous_id = str(uuid.uuid4())
        section["anonymous_id"] = anonymous_id
        section.setdefault("enabled", None)
        section.setdefault("consent_version", None)
        section.setdefault("consented_at", None)
        data[_TELEMETRY_KEY] = section
        save_config(data)

    enabled = section.get("enabled")
    if os.environ.get(NAURO_TELEMETRY_ENV) == "0":
        enabled = False

    return TelemetryConfig(
        anonymous_id=anonymous_id,
        enabled=enabled,
        consent_version=section.get("consent_version"),
        consented_at=section.get("consented_at"),
    )
