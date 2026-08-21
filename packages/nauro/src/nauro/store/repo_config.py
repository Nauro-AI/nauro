"""Repo-local Nauro config — ``<repo>/.nauro/config.json``.

Each repo associated with Nauro carries a small JSON file that identifies the
project the repo belongs to. Two modes are supported:

- ``local``: ``{"mode": "local", "id": <ulid>, "name": <str>, "schema_version": 1}``
- ``cloud``: ``{"mode": "cloud", "id": <ulid>, "name": <str>, "server_url": <str>,
  "schema_version": 1}``

The ``id`` field carries either a CLI-minted local ULID or a server-minted cloud
ULID; never both, never neither. The loader rejects unknown ``schema_version``
values with a clear error so old clients fail loudly when faced with future
schemas.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path

from nauro.constants import (
    REPO_CONFIG_DIR,
    REPO_CONFIG_FILENAME,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
    REPO_CONFIG_SCHEMA_VERSION,
)
from nauro.store._atomic import atomic_write_text
from nauro.store.home import config_file
from nauro.store.write_safety import find_symlink

logger = logging.getLogger("nauro.repo_config")

_VALID_MODES = (REPO_CONFIG_MODE_LOCAL, REPO_CONFIG_MODE_CLOUD)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LEN = 26


def _is_valid_ulid(value: str) -> bool:
    """True when ``value`` is a 26-char Crockford-base32 ULID. Trust-boundary check:
    the id from an untrusted ``.nauro/config.json`` becomes a path component, so
    this rejects traversal shapes before they reach the filesystem."""
    return len(value) == _ULID_LEN and all(ch in _CROCKFORD for ch in value)


class RepoConfigSchemaError(Exception):
    """Raised when a ``.nauro/config.json`` has an unknown schema_version or shape."""


class RepoConfigLocationError(Exception):
    """Raised when a repo config write targets Nauro's own global config file."""


class RepoConfigSymlinkError(Exception):
    """Raised when a repo config write would traverse a symlink in the checkout."""


def collides_with_global_config(repo_root: Path) -> bool:
    """True when ``repo_root``'s config path is Nauro's global config file.

    A repo config at ``Path.home()`` would replace the user's credentials, so writers refuse.
    """
    return repo_config_path(repo_root).resolve() == config_file().resolve()


def generate_ulid() -> str:
    """Generate a 26-char Crockford-base32 ULID: 48 bits of ms timestamp, 80 random.

    Minted CLI-side for local-only projects; cloud ids arrive from the server.
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(secrets.token_bytes(10), "big")
    value = (timestamp_ms << 80) | randomness
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def repo_config_path(repo_root: Path) -> Path:
    """Return the path where a repo's config file lives."""
    return repo_root / REPO_CONFIG_DIR / REPO_CONFIG_FILENAME


def _validate(data: dict) -> None:
    version = data.get("schema_version")
    if version != REPO_CONFIG_SCHEMA_VERSION:
        raise RepoConfigSchemaError(
            f"Unknown repo config schema_version={version!r}. "
            f"This nauro build understands schema_version={REPO_CONFIG_SCHEMA_VERSION}. "
            f"Upgrade nauro to a version that supports this schema."
        )

    mode = data.get("mode")
    if mode not in _VALID_MODES:
        raise RepoConfigSchemaError(
            f"Invalid repo config mode={mode!r}; expected one of {_VALID_MODES}."
        )

    if not isinstance(data.get("id"), str) or not data["id"]:
        raise RepoConfigSchemaError("Repo config is missing required field 'id'.")
    if not _is_valid_ulid(data["id"]):
        raise RepoConfigSchemaError(
            f"Repo config 'id' {data['id']!r} is not a valid ULID "
            f"({_ULID_LEN} Crockford-base32 chars). The id names a directory under "
            "the project store; a malformed value is refused so it cannot escape it."
        )
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise RepoConfigSchemaError("Repo config is missing required field 'name'.")

    if mode == REPO_CONFIG_MODE_CLOUD and (
        not isinstance(data.get("server_url"), str) or not data["server_url"]
    ):
        raise RepoConfigSchemaError(
            "Cloud-mode repo config is missing required field 'server_url'."
        )


def load_repo_config(repo_root: Path) -> dict:
    """Read ``<repo_root>/.nauro/config.json``; FileNotFoundError when absent.
    RepoConfigSchemaError covers unknown schema_version, missing fields, and corrupt
    JSON or non-UTF-8, remapped into one typed family so callers degrade on either."""
    path = repo_config_path(repo_root)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        logger.warning("Corrupt repo config at %s: %s", path, exc)
        raise RepoConfigSchemaError(f"Repo config at {path} is not valid UTF-8.") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Corrupt repo config at %s: %s", path, exc)
        raise RepoConfigSchemaError(f"Repo config at {path} is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise RepoConfigSchemaError(f"Repo config at {path} is not a JSON object.")
    _validate(data)
    return data


def save_repo_config(repo_root: Path, data: dict) -> Path:
    """Write the repo config atomically, returning the path written. An invalid shape raises
    RepoConfigSchemaError without touching disk; a symlink-traversing config path raises
    RepoConfigSymlinkError, checked first; a global-config collision, RepoConfigLocationError."""
    refusal = find_symlink(repo_root, f"{REPO_CONFIG_DIR}/{REPO_CONFIG_FILENAME}")
    if refusal is not None:
        raise RepoConfigSymlinkError(refusal.message)
    if collides_with_global_config(repo_root):
        raise RepoConfigLocationError(
            f"Refusing to write a repo config at {repo_config_path(repo_root)}: "
            "that path is Nauro's global config file, which holds user-level "
            "credentials and settings. Run from a project directory instead."
        )
    data.setdefault("schema_version", REPO_CONFIG_SCHEMA_VERSION)
    _validate(data)

    path = repo_config_path(repo_root)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return path


def find_repo_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for ``.nauro/config.json``.

    Returns the config path, or ``None`` at the filesystem root; ``start`` defaults to cwd.
    """
    current = (start if start is not None else Path.cwd()).resolve()
    while True:
        candidate = current / REPO_CONFIG_DIR / REPO_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
