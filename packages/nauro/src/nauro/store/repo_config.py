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
    CONFIG_FILENAME,
    DEFAULT_NAURO_HOME,
    NAURO_HOME_ENV,
    REPO_CONFIG_DIR,
    REPO_CONFIG_FILENAME,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
    REPO_CONFIG_SCHEMA_VERSION,
)
from nauro.store._atomic import atomic_write_text
from nauro.store.write_safety import find_symlink

logger = logging.getLogger("nauro.repo_config")

_VALID_MODES = (REPO_CONFIG_MODE_LOCAL, REPO_CONFIG_MODE_CLOUD)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LEN = 26


def _is_valid_ulid(value: str) -> bool:
    """True when ``value`` is a 26-char Crockford-base32 ULID.

    Mirrors the alphabet and length produced by :func:`generate_ulid` and by
    the server when it mints cloud project ids. This is a trust-boundary check:
    the ``id`` from an untrusted ``.nauro/config.json`` becomes a path component
    under ``~/.nauro/projects/``, so a value carrying ``..``, path separators,
    or an absolute path could relocate the store root. Constraining ``id`` to
    the ULID alphabet rejects all of those before they reach the filesystem.
    """
    return len(value) == _ULID_LEN and all(ch in _CROCKFORD for ch in value)


class RepoConfigSchemaError(Exception):
    """Raised when a ``.nauro/config.json`` has an unknown schema_version or shape."""


class RepoConfigLocationError(Exception):
    """Raised when a repo config write targets Nauro's own global config file."""


class RepoConfigSymlinkError(Exception):
    """Raised when a repo config write would traverse a symlink in the checkout."""


def _global_config_file() -> Path:
    """Path of the global config. Mirrors ``registry._nauro_home()``.

    Not imported from registry because registry imports this module;
    the resolution must stay in lockstep with it.
    """
    home = Path(os.environ.get(NAURO_HOME_ENV, Path.home() / DEFAULT_NAURO_HOME))
    return home / CONFIG_FILENAME


def collides_with_global_config(repo_root: Path) -> bool:
    """True when ``repo_root``'s config path is Nauro's global config file.

    With the default home layout, ``repo_config_path(Path.home())`` resolves to
    ``~/.nauro/config.json`` — the same file that holds auth tokens and
    telemetry consent for the whole machine. Writing a repo config there would
    replace those settings, so writers must refuse the path.
    """
    return repo_config_path(repo_root).resolve() == _global_config_file().resolve()


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
    """Read the repo config from ``<repo_root>/.nauro/config.json``.

    Raises:
        FileNotFoundError: When the config file does not exist.
        RepoConfigSchemaError: When the file is missing required fields,
            advertises an unknown schema_version, or is corrupt/unparseable
            JSON. Corrupt JSON is remapped here so a single typed error family
            covers both schema-mismatch and corruption, letting callers
            degrade gracefully on either.
    """
    path = repo_config_path(repo_root)
    text = path.read_text()
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
    """Write the repo config atomically. Returns the path written.

    The data dict is validated before write; an invalid shape raises
    RepoConfigSchemaError without touching disk. A ``repo_root`` whose config
    path collides with the global config raises RepoConfigLocationError —
    the last line of defense for every writer; CLI commands additionally
    refuse such paths up front with friendlier guidance. A config path that
    traverses a symlink inside the checkout (a symlinked ``.nauro`` directory
    or ``config.json``) raises RepoConfigSymlinkError under the same
    last-line-of-defense contract: a pre-planted link in a cloned repo would
    redirect the write outside the checkout.
    """
    if collides_with_global_config(repo_root):
        raise RepoConfigLocationError(
            f"Refusing to write a repo config at {repo_config_path(repo_root)}: "
            "that path is Nauro's global config file, which holds auth and "
            "telemetry settings. Run from a project directory instead."
        )
    refusal = find_symlink(repo_root, f"{REPO_CONFIG_DIR}/{REPO_CONFIG_FILENAME}")
    if refusal is not None:
        raise RepoConfigSymlinkError(refusal.message)
    data.setdefault("schema_version", REPO_CONFIG_SCHEMA_VERSION)
    _validate(data)

    path = repo_config_path(repo_root)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
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
