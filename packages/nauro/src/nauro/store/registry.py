"""Project registry — manages ~/.nauro/registry.json.

The registry maps project names to one or more associated repo paths on the
machine. The project store lives at ~/.nauro/projects/<project-name>/.

Respects NAURO_HOME env var override (defaults to ~/.nauro/).
"""

import json
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from nauro.constants import (
    DEFAULT_NAURO_HOME,
    NAURO_HOME_ENV,
    PROJECTS_DIR,
    REGISTRY_FILENAME,
    SCHEMA_VERSION,
)

logger = logging.getLogger("nauro.registry")


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
