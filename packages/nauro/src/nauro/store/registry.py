"""Project registry — manages ~/.nauro/registry.json.

Two on-disk shapes are supported during the project-scoped migration:

* **v1 (legacy)** — keyed by project name; store path ``~/.nauro/projects/<name>/``.
  Helpers: ``load_registry``, ``save_registry``, ``register_project``,
  ``resolve_project``, ``add_repo``, ``remove_repo``, ``find_stale_paths``,
  ``suggest_project_for_path``, ``get_project``, ``get_store_path``.
* **v2 (canonical post-migration)** — keyed by project_id (ULID), tracks
  ``mode`` + optional ``server_url``. Store path ``~/.nauro/projects/<id>/``.
  Helpers: ``load_registry_v2``, ``save_registry_v2``, ``register_project_v2``,
  ``get_store_path_v2``, ``get_project_v2``, ``find_projects_by_name_v2``,
  ``resolve_v2_from_path``, ``add_repo_v2``, ``rename_project_id_v2``.

The two shapes are mutually exclusive on disk; ``load_registry_v2`` rejects
v1 with a one-time manual migration message. v1 helpers are retained as
legacy for callers that have not yet been migrated.

Respects NAURO_HOME env var override (defaults to ~/.nauro/).
"""

import json
import logging
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from nauro.constants import (
    DEFAULT_NAURO_HOME,
    NAURO_HOME_ENV,
    PROJECTS_DIR,
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION_V1,
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
    SCHEMA_VERSION,
)
from nauro.store.repo_config import generate_ulid

logger = logging.getLogger("nauro.registry")


class RegistrySchemaError(Exception):
    """Raised when registry.json advertises a schema_version this build cannot read."""


@contextmanager
def _registry_lock():
    """Exclusive file lock on registry.json for atomic read-modify-write."""
    lock_path = _registry_file().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(lock_path):
        yield


def _nauro_home() -> Path:
    return Path(os.environ.get(NAURO_HOME_ENV, Path.home() / DEFAULT_NAURO_HOME))


def _registry_file() -> Path:
    return _nauro_home() / REGISTRY_FILENAME


def _projects_dir() -> Path:
    return _nauro_home() / PROJECTS_DIR


def load_registry() -> dict:
    """Read registry.json, return empty structure if it doesn't exist or is corrupt.

    Returns:
        Registry dict with a "projects" key mapping names to project entries.
    """
    rf = _registry_file()
    if rf.exists():
        try:
            data = json.loads(rf.read_text())
        except json.JSONDecodeError:
            logger.warning("registry.json is corrupt — starting with empty registry")
            return {"projects": {}, "schema_version": SCHEMA_VERSION}
        data.setdefault("schema_version", 1)
        return data  # type: ignore[no-any-return]
    return {"projects": {}, "schema_version": SCHEMA_VERSION}


def save_registry(data: dict) -> None:
    """Write registry.json atomically (write-to-tmp + rename).

    Args:
        data: Full registry dict to persist.
    """
    data.setdefault("schema_version", SCHEMA_VERSION)
    rf = _registry_file()
    rf.parent.mkdir(parents=True, exist_ok=True)
    tmp = rf.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, rf)


# ── v2 registry (id-keyed) ───────────────────────────────────────────────────
#
# v2 introduces a project_id (ULID) primary key and tracks ``mode`` plus an
# optional ``server_url`` per project. v2 is read/written by the new
# project-scoped commands shipping in 2c-B; existing v1 commands continue to
# use load_registry/save_registry above.
#
# v2 is a strict loader: it refuses to read a v1 registry and tells the user
# to run the one-time manual migration documented in 2c-B's release notes.
# Auto-migration is intentionally out of scope — solo-founder scale, single
# existing project, three shell commands beat code that needs idempotency,
# directory renames, and adopt-existing-config fallbacks.


def load_registry_v2() -> dict:
    """Read registry.json under v2 semantics. Refuses v1.

    Returns:
        Registry dict shaped as ``{"projects": {<id>: {...}}, "schema_version": 2}``.
        When the file does not exist, returns the empty v2 shape.

    Raises:
        RegistrySchemaError: If the on-disk registry is at schema_version 1 (a
            one-time manual migration is required) or any other unknown
            version.
    """
    rf = _registry_file()
    if not rf.exists():
        return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V2}

    try:
        data = json.loads(rf.read_text())
    except json.JSONDecodeError:
        logger.warning("registry.json is corrupt — starting with empty v2 registry")
        return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V2}

    version = data.get("schema_version", REGISTRY_SCHEMA_VERSION_V1)
    if version == REGISTRY_SCHEMA_VERSION_V1:
        raise RegistrySchemaError(
            "registry is at schema_version 1; please run the one-time manual "
            "migration documented in the release notes before continuing."
        )
    if version != REGISTRY_SCHEMA_VERSION_V2:
        raise RegistrySchemaError(
            f"Unknown registry schema_version={version!r} at {rf}. "
            f"Upgrade nauro to a version that supports this schema."
        )
    return data  # type: ignore[no-any-return]


def save_registry_v2(data: dict) -> None:
    """Write a v2 registry atomically. Stamps schema_version=2 on the data.

    Raises:
        RegistrySchemaError: If ``data["schema_version"]`` is set to anything
            other than 2.
    """
    data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION_V2)
    if data["schema_version"] != REGISTRY_SCHEMA_VERSION_V2:
        raise RegistrySchemaError(
            f"save_registry_v2 refuses to write schema_version="
            f"{data['schema_version']!r}; expected {REGISTRY_SCHEMA_VERSION_V2}."
        )
    rf = _registry_file()
    rf.parent.mkdir(parents=True, exist_ok=True)
    tmp = rf.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, rf)


def get_store_path(name: str) -> Path:
    """Return the store directory path for a project name."""
    return _projects_dir() / name


def get_project(name: str) -> dict | None:
    """Look up a project by name directly from the registry.

    Args:
        name: Project name to look up.

    Returns:
        Project entry dict or None if not found.
    """
    registry = load_registry()
    return registry["projects"].get(name)  # type: ignore[no-any-return]


def resolve_project(path: Path) -> str | None:
    """Given a directory path, find which project it belongs to.

    Walks up the directory tree and checks all registered repo paths for a match.

    Args:
        path: Directory path to resolve.

    Returns:
        Project name or None if no match found.
    """
    registry = load_registry()
    path = path.resolve()
    for name, entry in registry["projects"].items():
        for repo in entry["repo_paths"]:
            repo_resolved = Path(repo).resolve()
            if path == repo_resolved or repo_resolved in path.parents:
                return name  # type: ignore[no-any-return]
    return None


def register_project(name: str, repo_paths: list[Path]) -> Path:
    """Add a project to the registry and create its store directory.

    Args:
        name: Project name.
        repo_paths: List of associated repo paths.

    Returns:
        Path to the new project store directory.

    Raises:
        ValueError: If project already exists.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", name):
        raise ValueError(
            f"Invalid project name '{name}'. Use only letters, digits, hyphens, and underscores."
        )

    with _registry_lock():
        registry = load_registry()
        if name in registry["projects"]:
            raise ValueError(f"Project '{name}' already exists in registry.")
        registry["projects"][name] = {
            "repo_paths": [str(p.resolve()) for p in repo_paths],
        }
        store_path = _projects_dir() / name
        store_path.mkdir(parents=True, exist_ok=True)
        save_registry(registry)
    return store_path


def find_stale_paths() -> list[tuple[str, str]]:
    """Check all registered repo paths and return those that no longer exist.

    Returns:
        List of (project_name, path_string) tuples for missing paths.
    """
    registry = load_registry()
    stale = []
    for name, entry in registry["projects"].items():
        for repo_str in entry.get("repo_paths", []):
            if not Path(repo_str).is_dir():
                stale.append((name, repo_str))
    return stale


def suggest_project_for_path(path: Path) -> str | None:
    """Suggest a project that might match a path based on directory name.

    Useful when resolve_project() returns None — the repo may have moved.

    Args:
        path: Directory path to match.

    Returns:
        Project name if a plausible match is found, None otherwise.
    """
    registry = load_registry()
    path = path.resolve()
    dirname = path.name.lower()

    for name in registry["projects"]:
        if name.lower() == dirname:
            return name  # type: ignore[no-any-return]

    return None


def add_repo(project_name: str, repo_path: Path) -> None:
    """Add a repo path to an existing project.

    Args:
        project_name: Name of the project to update.
        repo_path: Repo path to associate.

    Raises:
        KeyError: If project doesn't exist.
    """
    with _registry_lock():
        registry = load_registry()
        if project_name not in registry["projects"]:
            raise KeyError(f"Project '{project_name}' not found in registry.")
        resolved = str(repo_path.resolve())
        if resolved not in registry["projects"][project_name]["repo_paths"]:
            registry["projects"][project_name]["repo_paths"].append(resolved)
        save_registry(registry)


def remove_repo(project_name: str, repo_path_str: str) -> bool:
    """Remove a repo path from an existing project.

    Args:
        project_name: Name of the project to update.
        repo_path_str: Repo path string to remove (exact match against stored value).

    Returns:
        True if the path was found and removed, False if not found.

    Raises:
        KeyError: If project doesn't exist.
    """
    with _registry_lock():
        registry = load_registry()
        if project_name not in registry["projects"]:
            raise KeyError(f"Project '{project_name}' not found in registry.")
        paths = registry["projects"][project_name]["repo_paths"]
        if repo_path_str in paths:
            paths.remove(repo_path_str)
            save_registry(registry)
            return True
    return False


# ── v2 registry CRUD (id-keyed) ──────────────────────────────────────────────


_VALID_MODES_V2 = (REPO_CONFIG_MODE_LOCAL, REPO_CONFIG_MODE_CLOUD)


def get_store_path_v2(project_id: str) -> Path:
    """Return the id-keyed store directory for a v2 project."""
    return _projects_dir() / project_id


def _load_registry_v2_or_empty() -> dict:
    """Read v2 registry; treat a v1-on-disk as 'no v2 entries'.

    Read paths use this so that lookups silently fall back to v1 helpers
    while a project is still on the v1 schema. Write paths continue to
    call ``load_registry_v2`` directly, so v1 → v2 mutations fail loudly
    rather than silently overwriting the v1 file.
    """
    try:
        return load_registry_v2()
    except RegistrySchemaError:
        return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V2}


def get_project_v2(project_id: str) -> dict | None:
    """Look up a v2 project entry by its project_id (ULID)."""
    registry = _load_registry_v2_or_empty()
    return registry["projects"].get(project_id)  # type: ignore[no-any-return]


def find_projects_by_name_v2(name: str) -> list[tuple[str, dict]]:
    """Return every v2 project whose ``name`` field matches ``name``.

    Returns a list because v2 allows duplicate names — id is the unique key.
    """
    registry = _load_registry_v2_or_empty()
    out: list[tuple[str, dict]] = []
    for pid, entry in registry["projects"].items():
        if entry.get("name") == name:
            out.append((pid, entry))
    return out


def resolve_v2_from_path(path: Path) -> tuple[str, dict] | None:
    """Walk up ``path`` and return (project_id, entry) for the matching v2 project.

    Mirrors v1 ``resolve_project`` but on the id-keyed v2 registry.
    """
    registry = _load_registry_v2_or_empty()
    path = path.resolve()
    for pid, entry in registry["projects"].items():
        for repo in entry.get("repo_paths", []):
            repo_resolved = Path(repo).resolve()
            if path == repo_resolved or repo_resolved in path.parents:
                return pid, entry
    return None


def register_project_v2(
    name: str,
    repo_paths: list[Path],
    *,
    mode: str = REPO_CONFIG_MODE_LOCAL,
    project_id: str | None = None,
    server_url: str | None = None,
) -> tuple[str, Path]:
    """Add a v2 project to the registry and create its id-keyed store directory.

    Args:
        name: Display name (need not be unique).
        repo_paths: List of associated repo paths.
        mode: ``"local"`` (CLI-minted ULID) or ``"cloud"`` (server-minted).
        project_id: ULID to use; minted via ``generate_ulid()`` when omitted.
        server_url: Required when mode == "cloud".

    Returns:
        Tuple of (project_id, store_path).

    Raises:
        ValueError: If mode/server_url combination is invalid, or if the
            project_id is already present in the registry.
    """
    if mode not in _VALID_MODES_V2:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {_VALID_MODES_V2}.")
    if mode == REPO_CONFIG_MODE_CLOUD and not server_url:
        raise ValueError("Cloud-mode v2 registration requires a server_url.")

    pid = project_id or generate_ulid()

    with _registry_lock():
        registry = load_registry_v2()
        if pid in registry["projects"]:
            raise ValueError(f"Project id {pid!r} is already registered.")
        entry: dict = {
            "name": name,
            "mode": mode,
            "repo_paths": [str(p.resolve()) for p in repo_paths],
        }
        if mode == REPO_CONFIG_MODE_CLOUD:
            entry["server_url"] = server_url
        registry["projects"][pid] = entry

        store_path = get_store_path_v2(pid)
        store_path.mkdir(parents=True, exist_ok=True)
        save_registry_v2(registry)
    return pid, store_path


def add_repo_v2(project_id: str, repo_path: Path) -> None:
    """Add a repo path to an existing v2 project.

    Raises:
        KeyError: If the project_id is not in the v2 registry.
    """
    with _registry_lock():
        registry = load_registry_v2()
        if project_id not in registry["projects"]:
            raise KeyError(f"Project id {project_id!r} not found in v2 registry.")
        resolved = str(repo_path.resolve())
        paths = registry["projects"][project_id].setdefault("repo_paths", [])
        if resolved not in paths:
            paths.append(resolved)
        save_registry_v2(registry)


def rename_project_id_v2(
    old_id: str,
    new_id: str,
    *,
    mode: str | None = None,
    server_url: str | None = None,
    rename_store: bool = True,
) -> Path:
    """Re-key a v2 project entry from ``old_id`` to ``new_id``.

    Used by ``nauro link --cloud`` to promote a local-only project to a
    cloud project: the store directory is renamed to the new id-keyed path
    and the registry entry is moved to the new key, preserving repo_paths.

    Args:
        old_id: Current project_id to migrate from.
        new_id: New project_id (typically a server-minted cloud ULID).
        mode: New mode value (e.g. ``"cloud"``); leave None to retain.
        server_url: New server_url; required if the resulting mode is cloud.
        rename_store: When True (default), also move the on-disk store
            directory from ``<projects>/old_id/`` to ``<projects>/new_id/``.

    Returns:
        Path to the renamed store directory.

    Raises:
        KeyError: If ``old_id`` is not in the v2 registry.
        ValueError: If ``new_id`` is already registered, or if the
            resulting mode/server_url combination is invalid.
    """
    with _registry_lock():
        registry = load_registry_v2()
        if old_id not in registry["projects"]:
            raise KeyError(f"Project id {old_id!r} not found in v2 registry.")
        if new_id != old_id and new_id in registry["projects"]:
            raise ValueError(f"Project id {new_id!r} is already registered.")

        entry = dict(registry["projects"].pop(old_id))
        if mode is not None:
            if mode not in _VALID_MODES_V2:
                raise ValueError(f"Invalid mode {mode!r}; expected one of {_VALID_MODES_V2}.")
            entry["mode"] = mode
        if server_url is not None:
            entry["server_url"] = server_url
        if entry.get("mode") == REPO_CONFIG_MODE_CLOUD and not entry.get("server_url"):
            raise ValueError("Cloud-mode entry requires a server_url.")
        if entry.get("mode") == REPO_CONFIG_MODE_LOCAL:
            entry.pop("server_url", None)
        registry["projects"][new_id] = entry

        old_store = get_store_path_v2(old_id)
        new_store = get_store_path_v2(new_id)
        if rename_store and old_id != new_id and old_store.exists():
            if new_store.exists():
                raise ValueError(f"Cannot rename store: destination already exists at {new_store}.")
            shutil.move(str(old_store), str(new_store))
        new_store.mkdir(parents=True, exist_ok=True)
        save_registry_v2(registry)
    return new_store
