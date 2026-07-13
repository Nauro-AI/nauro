"""User configuration — manages ~/.nauro/config.json.

Stores user-level settings (telemetry consent, anonymous_id, etc.).
Respects NAURO_HOME env var override (defaults to ~/.nauro/).
"""

import json
import logging
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from nauro.constants import (
    CONFIG_FILENAME,
    DEFAULT_NAURO_HOME,
    NAURO_EMBEDDINGS_ENV,
    NAURO_HOME_ENV,
    NAURO_TELEMETRY_ENV,
)
from nauro.store._atomic import atomic_write_text
from nauro.store.registry import _ensure_nauro_home

logger = logging.getLogger("nauro.config")

# Config key for the optional embedding retrieval augmenter. The env var
# NAURO_EMBEDDINGS overrides it, mirroring the NAURO_HOME precedence.
_EMBEDDINGS_CONFIG_KEY = "search.embeddings"


def _config_file() -> Path:
    nauro_home = Path(os.environ.get(NAURO_HOME_ENV, Path.home() / DEFAULT_NAURO_HOME))
    return nauro_home / CONFIG_FILENAME


@contextmanager
def _config_lock(timeout: float = -1):
    """Exclusive file lock on config.json for atomic read-modify-write.

    Mirrors ``registry._registry_lock``. The lock is NOT re-entrant — callers
    must never open a ``config_transaction`` inside another, or it deadlocks.
    ``timeout`` is forwarded to ``FileLock``: the default of -1 waits forever
    (every existing caller), while a non-negative bound raises
    ``filelock.Timeout`` on expiry so a stuck holder cannot block a caller
    indefinitely.
    """
    lock_path = _config_file().with_suffix(".lock")
    _ensure_nauro_home()  # lock_path.parent is the home dir; create it owner-only
    with FileLock(str(lock_path), timeout=timeout):
        yield


@contextmanager
def config_transaction(timeout: float = -1):
    """Lock, reload fresh, yield the working dict, then persist on clean exit.

    Binding the lock to a reload-and-save means a holder can never operate on a
    stale snapshot. A body that raises skips ``save_config`` entirely, leaving
    the file untouched. The lock is not re-entrant: a body must not open a
    second ``config_transaction`` (sequence the writes instead). ``timeout`` is
    forwarded to the lock: the default waits forever, a bound raises
    ``filelock.Timeout`` on expiry.
    """
    with _config_lock(timeout=timeout):
        data = load_config()
        yield data
        save_config(data)


def _quarantine_corrupt_config(cf: Path) -> None:
    """Preserve a corrupt config.json before a caller overwrites it.

    load_config returns {} on a corrupt file and config_transaction then
    persists that empty dict — which would destroy any hand-recoverable content
    (e.g. the auth tokens) in the broken file. Rename it to a timestamped
    sidecar first so the data survives, and tell the user where it went.
    Best-effort: a read-only dir or a concurrent rename is swallowed.
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
            "permissions) — returning empty config; back it up manually if it held "
            "credentials"
        )


def load_config() -> dict:
    """Read config.json, return empty dict if it doesn't exist or is corrupt.

    A corrupt or wrong-shape file is moved aside to a ``.corrupt-<ts>`` sidecar
    before returning {} so a subsequent save cannot silently destroy any
    recoverable content.
    """
    cf = _config_file()
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
    cf = _config_file()
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

    Uses the lock primitive directly rather than ``config_transaction`` so the
    missing-key path can return without rewriting the file.
    """
    with _config_lock():
        data = load_config()
        if key not in data:
            return False
        del data[key]
        save_config(data)
    return True


def resolve_embeddings_flag() -> bool:
    """Resolve whether embedding-augmented retrieval is enabled.

    Precedence (mirrors NAURO_HOME): the ``NAURO_EMBEDDINGS`` env var wins when
    set; otherwise the ``search.embeddings`` config key is consulted; otherwise
    the default is OFF. Env and config both accept the same truthy tokens
    (``"1"``, ``"true"``, ``"yes"``, ``"on"``, case-insensitive) and a native
    bool from config.
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
        # anonymous_id is generated and persisted before consent so the
        # consent record can attach to a stable identity that already exists.
        # The write goes through config_transaction so the persisted dict is
        # reloaded fresh under the lock rather than the snapshot read above.
        anonymous_id = str(uuid.uuid4())
        with config_transaction() as fresh:
            fresh_section = fresh.get(_TELEMETRY_KEY) or {}
            if fresh_section.get("anonymous_id"):
                # A concurrent caller already minted one; adopt it.
                anonymous_id = fresh_section["anonymous_id"]
            else:
                fresh_section["anonymous_id"] = anonymous_id
                fresh_section.setdefault("enabled", None)
                fresh_section.setdefault("consent_version", None)
                fresh_section.setdefault("consented_at", None)
                fresh[_TELEMETRY_KEY] = fresh_section
            section = fresh_section

    enabled = section.get("enabled")
    if os.environ.get(NAURO_TELEMETRY_ENV) == "0":
        enabled = False

    return TelemetryConfig(
        anonymous_id=anonymous_id,
        enabled=enabled,
        consent_version=section.get("consent_version"),
        consented_at=section.get("consented_at"),
    )
