"""Project registry — manages ~/.nauro/registry.json.

Two on-disk shapes are supported during the project-scoped migration:

* **v1 (legacy)** — keyed by project name; store path ``~/.nauro/projects/<name>/``.
  Helpers: ``load_registry``, ``save_registry``, ``register_project``,
  ``resolve_project``, ``suggest_project_for_path``, ``get_project``,
  ``get_store_path``.
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
from typing import Literal

from filelock import FileLock

from nauro.constants import (
    DECISIONS_DIR,
    DEFAULT_NAURO_HOME,
    NAURO_HOME_ENV,
    PROJECT_MD,
    PROJECTS_DIR,
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION_V1,
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
)
from nauro.store._atomic import atomic_write_text
from nauro.store.repo_config import generate_ulid

logger = logging.getLogger("nauro.registry")


class RegistrySchemaError(Exception):
    """Raised when registry.json advertises a schema_version this build cannot read."""


StoreBindingReason = Literal[
    "connected_record_missing",
    "connected_record_invalid",
    "connected_binding_conflict",
]


class StoreBindingError(ValueError):
    """A registry store binding cannot be used safely."""

    def __init__(self, reason_code: StoreBindingReason, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@contextmanager
def _registry_lock():
    """Exclusive file lock on registry.json for atomic read-modify-write."""
    lock_path = _registry_file().with_suffix(".lock")
    _ensure_nauro_home()  # lock_path.parent is the home dir; create it owner-only
    with FileLock(lock_path):
        yield


def _nauro_home() -> Path:
    return Path(os.environ.get(NAURO_HOME_ENV, Path.home() / DEFAULT_NAURO_HOME))


def _registry_file() -> Path:
    return _nauro_home() / REGISTRY_FILENAME


def _projects_dir() -> Path:
    return _nauro_home() / PROJECTS_DIR


def _ensure_nauro_home() -> Path:
    """Create the Nauro home dir (``~/.nauro`` or ``$NAURO_HOME``) owner-only.

    The home holds the auth token (``config.json``) and the full project store,
    so it must not be group/other-accessible. New installs are created at
    ``0o700``; a home created at the umask default by an older build is tightened
    in place. Deeper paths are created under the returned home with
    ``parents=True`` after this call, so the home never transits a wider mode.
    """
    home = _nauro_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if (home.stat().st_mode & 0o077) != 0:
            home.chmod(0o700)
    except OSError as exc:
        # Best-effort tightening of a pre-existing wide dir; a real failure
        # surfaces at the FileLock that follows. Log for diagnosis on locked-down
        # hosts rather than masking it entirely.
        logger.debug("Could not tighten %s to 0o700: %s", home, exc)
    return home


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
            logger.warning("registry.json is corrupt - starting with empty registry")
            return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V1}
        if not isinstance(data, dict):
            logger.warning("registry.json is corrupt - starting with empty registry")
            return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V1}
        data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION_V1)
        data.setdefault("projects", {})
        return data  # type: ignore[no-any-return]
    return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V1}


def save_registry(data: dict) -> None:
    """Write registry.json atomically (write-to-tmp + rename).

    Args:
        data: Full registry dict to persist.
    """
    data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION_V1)
    rf = _registry_file()
    atomic_write_text(rf, json.dumps(data, indent=2) + "\n")


# ── v2 registry (id-keyed) ───────────────────────────────────────────────────
#
# v2 introduces a project_id (ULID) primary key and tracks ``mode`` plus an
# optional ``server_url`` per project. v2 is read/written by the
# project-scoped commands; existing v1 commands continue to use
# load_registry/save_registry above.
#
# v2 is a strict loader: it refuses to read a v1 registry and tells the user
# to run the one-time manual migration documented in the release notes.
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
        logger.warning("registry.json is corrupt - starting with empty v2 registry")
        return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V2}
    if not isinstance(data, dict):
        logger.warning("registry.json is corrupt - starting with empty v2 registry")
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
    data.setdefault("projects", {})
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
    atomic_write_text(rf, json.dumps(data, indent=2) + "\n")


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
        for repo in entry.get("repo_paths", []):
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


# ── v2 registry CRUD (id-keyed) ──────────────────────────────────────────────


_VALID_MODES_V2 = (REPO_CONFIG_MODE_LOCAL, REPO_CONFIG_MODE_CLOUD)

_MAX_PROJECT_NAME_LEN = 100


def _validate_project_name(name: str) -> str:
    """Validate a v2 project name and return its stripped form.

    Rejects names that would corrupt the registry or escape the store
    directory layout. The store path is derived from the project_id (a
    ULID), not the name, but the name is persisted to ``registry.json`` and
    surfaced in repo configs and AGENTS.md, so it must stay printable and
    free of path-traversal substrings.

    Raises:
        ValueError: If the stripped name is empty, longer than 100 chars,
            contains a path separator (``/`` or ``\\``) or the ``..``
            substring, or contains a non-printable character.
    """
    name = name.strip()
    if not name:
        raise ValueError("Project name cannot be empty.")
    if len(name) > _MAX_PROJECT_NAME_LEN:
        raise ValueError(
            f"Project name is too long ({len(name)} chars); keep it under {_MAX_PROJECT_NAME_LEN}."
        )
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid project name {name!r}: must not contain '/', '\\', or '..'.")
    if any(not ch.isprintable() for ch in name):
        raise ValueError(
            f"Invalid project name {name!r}: must not contain non-printable characters."
        )
    return name


def get_store_path_v2(project_id: str) -> Path:
    """Return the id-keyed store directory for a v2 project.

    Defense-in-depth: ``project_id`` becomes a path component under the
    projects directory, so a value containing ``..`` or an absolute path could
    relocate the store outside ``~/.nauro/projects/``. The primary guard is
    ULID validation at the config trust boundary (``repo_config._validate``);
    this containment check ensures no caller — present or future, including
    ``nauro attach <project_id>`` taking the id straight from argv — can escape
    the projects root. It rejects only escapes, not the full ULID alphabet, so
    contained non-canonical ids (e.g. test fixtures) are left alone.
    """
    projects_root = _projects_dir()
    store_path = projects_root / project_id
    try:
        store_path.resolve().relative_to(projects_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"Refusing project_id {project_id!r}: resolves outside the project store."
        ) from exc
    return store_path


def _first_symlink_component(path: Path) -> Path | None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return current
    return None


def _validate_registered_store_path(
    project_id: str,
    store_path: Path,
    *,
    require_store: bool,
    strict_store: bool,
) -> Path:
    if not store_path.is_absolute():
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registered store path for {project_id!r} must be absolute.",
        )
    resolved = store_path.resolve(strict=False)
    if resolved != store_path:
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registered store path for {project_id!r} is not canonical: {store_path}.",
        )
    symlink = _first_symlink_component(store_path)
    if symlink is not None:
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registered store path for {project_id!r} traverses symlink {symlink}.",
        )
    if store_path.name != project_id:
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registered store path must end in project id {project_id!r}.",
        )

    default_store = get_store_path_v2(project_id)
    if store_path != default_store and default_store.exists():
        raise StoreBindingError(
            "connected_binding_conflict",
            f"Both the registered external store and default store exist for {project_id!r}.",
        )

    if not store_path.exists():
        if require_store:
            raise StoreBindingError(
                "connected_record_missing",
                f"Registered store path does not exist: {store_path}.",
            )
        return store_path
    if not store_path.is_dir():
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registered store path is not a directory: {store_path}.",
        )
    if strict_store and (
        not (store_path / PROJECT_MD).is_file()
        or (store_path / PROJECT_MD).is_symlink()
        or not (store_path / DECISIONS_DIR).is_dir()
        or (store_path / DECISIONS_DIR).is_symlink()
    ):
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registered path does not contain a valid Nauro store: {store_path}.",
        )
    return store_path


def resolve_registered_store_path_v2(
    project_id: str,
    *,
    require_store: bool = True,
) -> Path:
    """Resolve a v2 entry through the registry-aware store boundary."""
    entry = get_project_v2(project_id)
    if entry is None:
        raise KeyError(f"Project id {project_id!r} not found in v2 registry.")
    raw_store_path = entry.get("store_path")
    store_path = Path(raw_store_path) if raw_store_path else get_store_path_v2(project_id)
    return _validate_registered_store_path(
        project_id,
        store_path,
        require_store=require_store,
        strict_store=raw_store_path is not None,
    )


def bind_project_store_v2(
    *,
    project_id: str,
    name: str,
    mode: str,
    repo_path: Path,
    store_path: Path,
    server_url: str | None = None,
) -> Path:
    """Bind a validated local store to a v2 project without creating a store."""
    name = _validate_project_name(name)
    if mode not in _VALID_MODES_V2:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {_VALID_MODES_V2}.")
    if mode == REPO_CONFIG_MODE_CLOUD and not server_url:
        raise ValueError("Cloud-mode v2 binding requires a server_url.")
    store_path = _validate_registered_store_path(
        project_id,
        store_path,
        require_store=True,
        strict_store=True,
    )

    with _registry_lock():
        registry = load_registry_v2()
        existing = registry["projects"].get(project_id)
        if existing is not None:
            if existing.get("name") != name or existing.get("mode") != mode:
                raise StoreBindingError(
                    "connected_binding_conflict",
                    f"Registry identity for {project_id!r} conflicts with the repository config.",
                )
            if mode == REPO_CONFIG_MODE_CLOUD and existing.get("server_url") != server_url:
                raise StoreBindingError(
                    "connected_binding_conflict",
                    f"Registry server for {project_id!r} conflicts with the repository config.",
                )
            try:
                current = resolve_registered_store_path_v2(project_id, require_store=False)
            except StoreBindingError as exc:
                if exc.reason_code == "connected_binding_conflict":
                    raise
                current = None
            if current is not None and current != store_path and current.exists():
                raise StoreBindingError(
                    "connected_binding_conflict",
                    f"Project {project_id!r} is already bound to {current}.",
                )
            entry = dict(existing)
        else:
            entry = {"name": name, "mode": mode, "repo_paths": []}

        resolved_repo = str(repo_path.resolve())
        repo_paths = list(entry.get("repo_paths", []))
        if resolved_repo not in repo_paths:
            repo_paths.append(resolved_repo)
        entry["repo_paths"] = repo_paths
        if mode == REPO_CONFIG_MODE_CLOUD:
            entry["server_url"] = server_url
        else:
            entry.pop("server_url", None)
        if store_path == get_store_path_v2(project_id):
            entry.pop("store_path", None)
        else:
            entry["store_path"] = str(store_path)
        registry["projects"][project_id] = entry
        save_registry_v2(registry)
    return store_path


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


def is_cloud_project(project_id: str) -> bool:
    """True iff ``project_id`` is a v2 cloud-mode registry entry.

    v1 entries (no ``mode`` field) and v2 local-mode entries return False.
    Used by the auto-sync hooks and the status command to gate presign
    calls — v1 has no server-side ULID and v2 local-mode is not
    remote-backed, so neither has a presign target.
    """
    try:
        entry = get_project_v2(project_id)
    except RegistrySchemaError:
        return False
    if entry is None:
        return False
    return entry.get("mode") == REPO_CONFIG_MODE_CLOUD


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


def get_repo_paths(project_key: str) -> list[str]:
    """Return repo paths for ``project_key`` from v2 (preferred) or v1 registry.

    ``project_key`` is a v2 project_id (ULID) or a v1 project name; v2 takes
    priority with v1 as the legacy fallback. Returns an empty list when the
    key is unknown in both schemas.
    """
    try:
        v2_entry = get_project_v2(project_key)
    except RegistrySchemaError:
        v2_entry = None
    if v2_entry is not None:
        return list(v2_entry.get("repo_paths", []))
    registry = load_registry()
    return list(registry["projects"].get(project_key, {}).get("repo_paths", []))


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
        ValueError: If the name is invalid, the mode/server_url combination
            is invalid, or the project_id is already present in the registry.
    """
    name = _validate_project_name(name)
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


def remove_repo_v2(project_id: str, repo_path_str: str) -> bool:
    """Remove one repo path from a v2 project, leaving the project entry intact.

    The mirror of ``add_repo_v2`` for teardown: ``nauro adopt --remove`` calls
    it when un-adopting one repo of a multi-repo project, so the project and its
    other repos survive. ``repo_path_str`` is matched exactly against the stored
    value (registration stores ``str(path.resolve())``).

    Returns:
        True if the path was found and removed, False if the project is unknown
        or the path was not associated with it.
    """
    with _registry_lock():
        registry = load_registry_v2()
        entry = registry["projects"].get(project_id)
        if entry is None:
            return False
        paths = entry.get("repo_paths", [])
        if repo_path_str not in paths:
            return False
        paths.remove(repo_path_str)
        save_registry_v2(registry)
    return True


def remove_project_v2(project_id: str) -> bool:
    """Remove a v2 project's registry entry. Leaves the on-disk store intact.

    The store directory under ``~/.nauro/projects/<id>/`` is deliberately
    preserved so a mistaken removal does not destroy decision history; the
    caller is responsible for surfacing where that data still lives.

    Returns:
        True if an entry with ``project_id`` existed and was removed, False
        if no such entry was present.
    """
    with _registry_lock():
        registry = load_registry_v2()
        if project_id not in registry["projects"]:
            return False
        registry["projects"].pop(project_id)
        save_registry_v2(registry)
    return True


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
        raw_store_path = entry.get("store_path")
        old_store = Path(raw_store_path) if raw_store_path else get_store_path_v2(old_id)
        _validate_registered_store_path(
            old_id,
            old_store,
            require_store=rename_store,
            strict_store=raw_store_path is not None,
        )
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

        new_store = old_store.with_name(new_id) if raw_store_path else get_store_path_v2(new_id)
        if raw_store_path and get_store_path_v2(new_id).exists():
            raise StoreBindingError(
                "connected_binding_conflict",
                f"Default store already exists for new project id {new_id!r}.",
            )
        if rename_store and old_id != new_id and old_store.exists():
            if new_store.exists():
                raise ValueError(f"Cannot rename store: destination already exists at {new_store}.")
            shutil.move(str(old_store), str(new_store))
        if raw_store_path:
            entry["store_path"] = str(new_store)
        else:
            entry.pop("store_path", None)
        registry["projects"][new_id] = entry
        save_registry_v2(registry)
    return new_store
