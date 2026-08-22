"""Project registry — manages ~/.nauro/registry.json.

The registry is keyed by project_id (ULID) and tracks ``name``, ``mode``,
``repo_paths``, an optional ``server_url``, and an optional external
``store_path`` per project (schema_version 2). Store path:
``~/.nauro/projects/<id>/``. Helpers: ``load_registry_v2``,
``save_registry_v2``, ``register_project_v2``, ``get_store_path_v2``,
``get_project_v2``, ``find_projects_by_name_v2``, ``resolve_v2_from_path``,
``add_repo_v2``, ``rename_project_id_v2``.

The retired name-keyed schema_version 1 shape is no longer readable:
``load_registry_v2`` rejects it with a manual-migration hint, and read
paths degrade to an empty registry.
"""

import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, model_validator

from nauro.constants import (
    DECISIONS_DIR,
    PROJECT_MD,
    REGISTRY_SCHEMA_VERSION_V1,
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
)
from nauro.store._atomic import atomic_write_text
from nauro.store.home import ensure_nauro_home, projects_dir, registry_file
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


class RegistryEntryV2(BaseModel):
    """Validated in-memory view of one schema-v2 registry entry."""

    model_config = ConfigDict(extra="allow", frozen=True)

    name: StrictStr
    mode: Literal["local", "cloud"]
    repo_paths: tuple[StrictStr, ...] = ()
    server_url: StrictStr | None = None
    store_path: StrictStr | None = None

    @model_validator(mode="after")
    def validate_cloud_server(self) -> "RegistryEntryV2":
        if self.mode == REPO_CONFIG_MODE_CLOUD and not (self.server_url or "").strip():
            raise ValueError("cloud registry entries require a nonempty server_url")
        return self

    @property
    def has_store_path(self) -> bool:
        return "store_path" in self.model_fields_set


def validate_registry_entry_v2(project_id: str, raw_entry: object) -> RegistryEntryV2:
    """Parse an untrusted registry entry into its typed in-memory form."""
    try:
        return RegistryEntryV2.model_validate(raw_entry)
    except ValidationError as exc:
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registry entry for {project_id!r} has an invalid schema.",
        ) from exc


@contextmanager
def _registry_lock():
    """Exclusive file lock on registry.json for atomic read-modify-write."""
    lock_path = registry_file().with_suffix(".lock")
    ensure_nauro_home()  # lock_path.parent is the home dir; create it owner-only
    with FileLock(lock_path):
        yield


# ── v2 registry (id-keyed) ───────────────────────────────────────────────────


def load_registry_v2() -> dict:
    """Read registry.json under v2 semantics; a missing file, invalid JSON, or a
    non-dict document returns the empty v2 shape. Raises RegistrySchemaError on a v1
    registry (manual migration required) or any other unknown schema_version."""
    rf = registry_file()
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
    """Write a v2 registry atomically, stamping schema_version=2; raises
    RegistrySchemaError when ``data`` carries any other schema_version."""
    data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION_V2)
    if data["schema_version"] != REGISTRY_SCHEMA_VERSION_V2:
        raise RegistrySchemaError(
            f"save_registry_v2 refuses to write schema_version="
            f"{data['schema_version']!r}; expected {REGISTRY_SCHEMA_VERSION_V2}."
        )
    rf = registry_file()
    atomic_write_text(rf, json.dumps(data, indent=2) + "\n")


# ── v2 registry CRUD (id-keyed) ──────────────────────────────────────────────


_VALID_MODES_V2 = (REPO_CONFIG_MODE_LOCAL, REPO_CONFIG_MODE_CLOUD)

_MAX_PROJECT_NAME_LEN = 100


def _validate_project_name(name: str) -> str:
    """Return the stripped project name, or raise ``ValueError``.

    Rejects empty, over 100 chars, a path separator, ``..``, or a non-printable.
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

    Rejects only a ``project_id`` that escapes the projects root, not a non-canonical id.
    """
    projects_root = projects_dir()
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

    return _validate_store_structure(
        project_id,
        store_path,
        require_store=require_store,
        strict_store=strict_store,
    )


def _validate_store_structure(
    project_id: str,
    store_path: Path,
    *,
    require_store: bool,
    strict_store: bool,
) -> Path:
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
    # Symlinked store components are refused in BOTH modes: tolerance for
    # the managed default path means components may be absent, never that a
    # pre-planted link may redirect store reads or sync writes elsewhere.
    if (store_path / PROJECT_MD).is_symlink() or (store_path / DECISIONS_DIR).is_symlink():
        raise StoreBindingError(
            "connected_record_invalid",
            f"Store components must not be symlinks: {store_path}.",
        )
    if strict_store and (
        not (store_path / PROJECT_MD).is_file() or not (store_path / DECISIONS_DIR).is_dir()
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
    registry_entry: RegistryEntryV2 | None = None,
) -> Path:
    """Resolve a v2 entry through the registry-aware store boundary."""
    entry = registry_entry or get_project_entry_v2(project_id)
    if entry is None:
        raise KeyError(f"Project id {project_id!r} not found in v2 registry.")

    # Strict structural validation is an external-binding rule: a
    # mapped path must contain a valid Nauro store. The default home path is
    # Nauro-managed and keeps its pre-recovery tolerance — an existing but
    # structurally incomplete default store resolves, and downstream tools
    # degrade gracefully, instead of dead-ending every command in a
    # connected_record_invalid state that reconnect cannot restore.
    if not entry.has_store_path:
        return _validate_store_structure(
            project_id,
            get_store_path_v2(project_id),
            require_store=require_store,
            strict_store=False,
        )

    raw_store_path = entry.store_path
    if raw_store_path is None or not raw_store_path.strip():
        raise StoreBindingError(
            "connected_record_invalid",
            f"Registered store path for {project_id!r} must be a nonempty string.",
        )
    return _validate_registered_store_path(
        project_id,
        Path(raw_store_path),
        require_store=require_store,
        strict_store=True,
    )


def registered_store_path_hint_v2(project_id: str, entry: object) -> Path | None:
    """Return a display-only store hint without trusting malformed registry data."""
    try:
        typed_entry = validate_registry_entry_v2(project_id, entry)
    except StoreBindingError:
        return None
    if not typed_entry.has_store_path:
        return get_store_path_v2(project_id)
    raw_store_path = typed_entry.store_path
    if raw_store_path is None or not raw_store_path.strip():
        return None
    return Path(raw_store_path)


def bind_project_store_v2(
    *,
    project_id: str,
    name: str,
    mode: str,
    repo_path: Path,
    store_path: Path,
    server_url: str | None = None,
    update_name: bool = False,
) -> Path:
    """Bind an existing, validated local store to a v2 project, creating the registry entry
    if needed but never the store directory. ``update_name`` is reserved for callers
    holding the authoritative (cloud) name; repo config never overwrites registry identity."""
    name = _validate_project_name(name)
    if mode not in _VALID_MODES_V2:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {_VALID_MODES_V2}.")
    if mode == REPO_CONFIG_MODE_CLOUD and not server_url:
        raise ValueError("Cloud-mode v2 binding requires a server_url.")
    default_store = get_store_path_v2(project_id)
    if store_path == default_store:
        # The default home path keeps its pre-recovery tolerance (see
        # resolve_registered_store_path_v2): binding it requires existence,
        # not full structure, so first connection to an empty cloud record
        # can bind the empty mirror directory that sync will populate.
        store_path = _validate_store_structure(
            project_id,
            store_path,
            require_store=True,
            strict_store=False,
        )
    else:
        store_path = _validate_registered_store_path(
            project_id,
            store_path,
            require_store=True,
            strict_store=True,
        )

    with _registry_lock():
        registry = load_registry_v2()
        raw_existing = registry["projects"].get(project_id)
        if raw_existing is not None:
            existing = validate_registry_entry_v2(project_id, raw_existing)
            if existing.mode != mode or (existing.name != name and not update_name):
                raise StoreBindingError(
                    "connected_binding_conflict",
                    f"Registry identity for {project_id!r} conflicts with the repository config.",
                )
            if mode == REPO_CONFIG_MODE_CLOUD and existing.server_url != server_url:
                raise StoreBindingError(
                    "connected_binding_conflict",
                    f"Registry server for {project_id!r} conflicts with the repository config.",
                )
            try:
                current = resolve_registered_store_path_v2(
                    project_id,
                    require_store=False,
                    registry_entry=existing,
                )
            except StoreBindingError as exc:
                if exc.reason_code == "connected_binding_conflict":
                    raise
                current = None
            if current is not None and current != store_path and current.exists():
                raise StoreBindingError(
                    "connected_binding_conflict",
                    f"Project {project_id!r} is already bound to {current}.",
                )
            entry = existing.model_dump(mode="json", exclude_unset=True)
            if update_name:
                entry["name"] = name
            repo_paths = list(existing.repo_paths)
        else:
            entry = {"name": name, "mode": mode, "repo_paths": []}
            repo_paths = []

        resolved_repo = str(repo_path.resolve())
        if resolved_repo not in repo_paths:
            repo_paths.append(resolved_repo)
        entry["repo_paths"] = repo_paths
        if mode == REPO_CONFIG_MODE_CLOUD:
            entry["server_url"] = server_url
        else:
            entry.pop("server_url", None)
        if store_path == default_store:
            entry.pop("store_path", None)
        else:
            entry["store_path"] = str(store_path)
        registry["projects"][project_id] = entry
        save_registry_v2(registry)
    return store_path


def _load_registry_v2_or_empty() -> dict:
    """Read-path helper: RegistrySchemaError degrades to the empty v2 shape.
    Write paths call ``load_registry_v2`` directly so a v1 or unknown-version
    registry fails loudly instead of being silently overwritten."""
    try:
        return load_registry_v2()
    except RegistrySchemaError:
        return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V2}


def get_project_v2(project_id: str) -> dict | None:
    """Look up a v2 project entry by its project_id (ULID)."""
    registry = _load_registry_v2_or_empty()
    return registry["projects"].get(project_id)  # type: ignore[no-any-return]


def get_project_entry_v2(project_id: str) -> RegistryEntryV2 | None:
    """Return one v2 registry entry after validating its boundary shape."""
    entry = get_project_v2(project_id)
    if entry is None:
        return None
    return validate_registry_entry_v2(project_id, entry)


def is_cloud_project(project_id: str) -> bool:
    """True iff ``project_id`` has a cloud-mode registry entry; unknown ids
    and local-mode entries return False."""
    entry = get_project_v2(project_id)
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
    """Return repo paths for ``project_key`` (a project_id ULID).

    Returns an empty list when the key is unknown.
    """
    entry = get_project_v2(project_key)
    if entry is None:
        return []
    return list(entry.get("repo_paths", []))


def resolve_v2_from_path(path: Path) -> tuple[str, dict] | None:
    """Walk up ``path`` and return (project_id, entry) for the matching project."""
    registry = _load_registry_v2_or_empty()
    path = path.resolve()
    for pid, entry in registry["projects"].items():
        for repo in entry.get("repo_paths", []):
            repo_resolved = Path(repo).resolve()
            if path == repo_resolved or repo_resolved in path.parents:
                return pid, entry
    return None


def suggest_project_for_path(path: Path) -> tuple[str, dict] | None:
    """Suggest the project whose name case-insensitively matches ``path``'s
    directory name. Returns ``(project_id, entry)`` only when exactly one
    project matches; an ambiguous or absent name yields None."""
    registry = _load_registry_v2_or_empty()
    dirname = path.resolve().name.lower()
    matches = [
        (pid, entry)
        for pid, entry in registry["projects"].items()
        if entry.get("name", "") and entry["name"].lower() == dirname
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def register_project_v2(
    name: str,
    repo_paths: list[Path],
    *,
    mode: str = REPO_CONFIG_MODE_LOCAL,
    project_id: str | None = None,
    server_url: str | None = None,
) -> tuple[str, Path]:
    """Add a v2 project to the registry and return ``(project_id, store_path)``.
    ``project_id`` is minted when absent, ``mode`` is ``local`` or ``cloud``, and
    ``cloud`` requires ``server_url``; a duplicate id or bad name raises ``ValueError``.
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
    """Add a repo path to an existing v2 project; raises KeyError on an unknown id."""
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
    ``repo_path_str`` is matched exactly against the stored resolved string. Returns
    False when the project is unknown or the path is not associated with it."""
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
    """Remove a v2 project's registry entry, preserving the on-disk store.
    Returns True when an entry existed and was removed, False otherwise."""
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
    """Re-key a v2 project from ``old_id`` to ``new_id``, preserving repo_paths; the store
    directory moves only when ``rename_store`` is set, the ids differ, and it exists.
    KeyError on unknown old_id; ValueError on a registered new_id or invalid mode/server_url."""
    with _registry_lock():
        registry = load_registry_v2()
        if old_id not in registry["projects"]:
            raise KeyError(f"Project id {old_id!r} not found in v2 registry.")
        if new_id != old_id and new_id in registry["projects"]:
            raise ValueError(f"Project id {new_id!r} is already registered.")

        existing = validate_registry_entry_v2(old_id, registry["projects"].pop(old_id))
        entry = existing.model_dump(mode="json", exclude_unset=True)
        if not existing.has_store_path:
            raw_store_path = None
            old_store = _validate_store_structure(
                old_id,
                get_store_path_v2(old_id),
                require_store=rename_store,
                strict_store=True,
            )
        else:
            raw_store_path = existing.store_path
            if raw_store_path is None or not raw_store_path.strip():
                raise StoreBindingError(
                    "connected_record_invalid",
                    f"Registered store path for {old_id!r} must be a nonempty string.",
                )
            old_store = _validate_registered_store_path(
                old_id,
                Path(raw_store_path),
                require_store=rename_store,
                strict_store=True,
            )
        if mode is not None:
            if mode not in _VALID_MODES_V2:
                raise ValueError(f"Invalid mode {mode!r}; expected one of {_VALID_MODES_V2}.")
            entry["mode"] = mode
        updated_mode = mode or existing.mode
        updated_server_url = server_url if server_url is not None else existing.server_url
        if updated_mode == REPO_CONFIG_MODE_CLOUD and not updated_server_url:
            raise ValueError("Cloud-mode entry requires a server_url.")
        if server_url is not None:
            entry["server_url"] = server_url
        if updated_mode == REPO_CONFIG_MODE_LOCAL:
            entry.pop("server_url", None)
        entry = validate_registry_entry_v2(old_id, entry).model_dump(
            mode="json", exclude_unset=True
        )

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
