"""User configuration — manages ~/.nauro/config.json.

Stores user-level settings like API keys. Config values for known keys
(e.g. api_key) are applied to the environment at CLI startup so that
downstream code (Anthropic SDK) picks them up automatically.

Respects NAURO_HOME env var override (defaults to ~/.nauro/).
"""

import json
import logging
import os
from pathlib import Path

from nauro.constants import CONFIG_FILENAME, DEFAULT_NAURO_HOME, NAURO_HOME_ENV

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
