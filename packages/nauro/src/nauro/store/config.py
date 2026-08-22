"""User configuration — manages ~/.nauro/config.json.

Stores user-level settings such as authentication and retrieval preferences.
The file path comes from ``nauro.store.home``.
"""

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from nauro.constants import NAURO_EMBEDDINGS_ENV
from nauro.store._atomic import atomic_write_text
from nauro.store.home import config_file, ensure_nauro_home

logger = logging.getLogger("nauro.config")

# Config key for the optional embedding retrieval augmenter. The env var
# NAURO_EMBEDDINGS overrides it, mirroring the home-path precedence.
_EMBEDDINGS_CONFIG_KEY = "search.embeddings"


@contextmanager
def _config_lock(timeout: float = -1):
    """Exclusive file lock on config.json for atomic read-modify-write; NOT re-entrant, so
    nesting (or ``config_transaction`` inside it) deadlocks. ``timeout`` forwards to ``FileLock``:
    default -1 waits forever; a non-negative bound raises ``filelock.Timeout`` on expiry.
    """
    lock_path = config_file().with_suffix(".lock")
    ensure_nauro_home()  # lock_path.parent is the home dir; create it owner-only
    with FileLock(str(lock_path), timeout=timeout):
        yield


@contextmanager
def config_transaction(timeout: float = -1):
    """Lock, reload fresh, yield the working dict, then persist on clean exit.

    Not re-entrant, and a body that raises skips the save, leaving the file untouched.
    """
    with _config_lock(timeout=timeout):
        data = load_config()
        yield data
        save_config(data)


def _quarantine_corrupt_config(cf: Path) -> None:
    """Rename a corrupt config.json aside before a caller overwrites it with ``{}``.

    Timestamped sidecar, best-effort: a read-only dir or a racing rename is swallowed.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    sidecar = cf.with_name(f"{cf.name}.corrupt-{ts}")
    try:
        cf.rename(sidecar)
        logger.warning(
            "config.json was unreadable; preserved a copy at %s and started a "
            "fresh config. If you were logged in, re-run `nauro auth login`.",
            sidecar,
        )
    except OSError:
        # Could not move the broken file aside (e.g. a read-only dir). It stays
        # on disk and a later save may overwrite it, so flag that the tokens may
        # still be at risk rather than implying a clean recovery.
        logger.warning(
            "config.json is corrupt and could not be preserved (check directory "
            "permissions) - returning empty config; back it up manually if it held "
            "credentials"
        )


def load_config() -> dict:
    """Read config.json, returning ``{}`` when it is missing, invalid JSON, or not a dict.
    An invalid-JSON or non-dict file is first moved aside to a ``.corrupt-<ts>`` sidecar,
    best-effort, before ``{}`` is returned; read and decode errors propagate.
    """
    cf = config_file()
    if cf.exists():
        try:
            data = json.loads(cf.read_text())
        except json.JSONDecodeError:
            _quarantine_corrupt_config(cf)
            return {}
        if not isinstance(data, dict):
            _quarantine_corrupt_config(cf)
            return {}
        return data  # type: ignore[no-any-return]
    return {}


def save_config(data: dict) -> None:
    """Write config.json atomically (write-to-tmp + rename). Restricts to owner-only (0o600)."""
    cf = config_file()
    atomic_write_text(cf, json.dumps(data, indent=2) + "\n", mode=0o600)


def get_config(key: str) -> str | None:
    """Get a single config value by key."""
    return load_config().get(key)


def set_config(key: str, value: str) -> None:
    """Set a single config value."""
    with config_transaction() as data:
        data[key] = value


def unset_config(key: str) -> bool:
    """Remove a config key. Returns True if the key existed.

    Uses the lock primitive directly so a missing key returns without a rewrite.
    """
    with _config_lock():
        data = load_config()
        if key not in data:
            return False
        del data[key]
        save_config(data)
    return True


def resolve_embeddings_flag() -> bool:
    """Resolve whether embedding-augmented retrieval is on: ``NAURO_EMBEDDINGS`` wins when set
    (a falsy env value disables even over a truthy config key), else the ``search.embeddings`` key,
    else OFF. Both accept ``1/true/yes/on`` case-insensitively; config also accepts a native bool.
    """
    env_value = os.environ.get(NAURO_EMBEDDINGS_ENV)
    if env_value is not None:
        return _is_truthy(env_value)
    return _is_truthy(get_config(_EMBEDDINGS_CONFIG_KEY))


def _is_truthy(value: object) -> bool:
    """Interpret a config/env value as a boolean flag."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False
