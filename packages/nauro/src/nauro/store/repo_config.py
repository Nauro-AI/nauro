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
import os
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

logger = logging.getLogger("nauro.repo_config")

_VALID_MODES = (REPO_CONFIG_MODE_LOCAL, REPO_CONFIG_MODE_CLOUD)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class RepoConfigSchemaError(Exception):
    """Raised when a ``.nauro/config.json`` has an unknown schema_version or shape."""


def generate_ulid() -> str:
    """Generate a 26-char Crockford-base32 ULID.

    The format is the standard ULID: 48 bits of millisecond timestamp followed
    by 80 bits of randomness. We mint these CLI-side for local-only projects;
    cloud project IDs are minted by the server and arrive over the wire.
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
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise RepoConfigSchemaError("Repo config is missing required field 'name'.")

    if mode == REPO_CONFIG_MODE_CLOUD:
        if not isinstance(data.get("server_url"), str) or not data["server_url"]:
            raise RepoConfigSchemaError(
                "Cloud-mode repo config is missing required field 'server_url'."
            )


def load_repo_config(repo_root: Path) -> dict:
    """Read the repo config from ``<repo_root>/.nauro/config.json``.

    Raises:
        FileNotFoundError: When the config file does not exist.
        RepoConfigSchemaError: When the file is missing required fields or
            advertises an unknown schema_version.
        json.JSONDecodeError: When the file is not valid JSON.
    """
    path = repo_config_path(repo_root)
    text = path.read_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RepoConfigSchemaError(f"Repo config at {path} is not a JSON object.")
    _validate(data)
    return data


def save_repo_config(repo_root: Path, data: dict) -> Path:
    """Write the repo config atomically. Returns the path written.

    The data dict is validated before write; an invalid shape raises
    RepoConfigSchemaError without touching disk.
    """
    data.setdefault("schema_version", REPO_CONFIG_SCHEMA_VERSION)
    _validate(data)

    path = repo_config_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def find_repo_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for ``.nauro/config.json``.

    Mirrors how git locates ``.git`` from anywhere inside a working tree.
    Stops at the filesystem root and returns ``None`` if no config is found.

    Args:
        start: Directory to start walking from. Defaults to the current
            working directory.

    Returns:
        The path of the config file when found, or ``None``.
    """
    current = (start if start is not None else Path.cwd()).resolve()
    while True:
        candidate = current / REPO_CONFIG_DIR / REPO_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
