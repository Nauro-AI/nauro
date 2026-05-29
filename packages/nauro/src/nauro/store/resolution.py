"""Store-resolution helper + typed exceptions.

The local stdio MCP transport translates a ``(project_id, cwd)`` pair into a
path under the active ``NAURO_HOME``. This module owns the resolution rules
and surfaces every failure as a typed exception so the transport can decide
whether to show the welcome screen or return a specific error message.

Resolution order:

  1. cwd's ``.nauro/config.json`` walk-up (id-keyed v2 store).
  2. ``project_id`` argument matched against v2 registry by name.
  3. ``project_id`` argument matched against v1 registry by name (legacy).
  4. ``cwd`` argument → v1 ``resolve_project`` (legacy).

Previously both transports raised a bare ``ValueError`` and the wrappers
swallowed every failure into the same ``WELCOME_NO_PROJECT`` onboarding
screen. The typed subclasses below let the wrappers reserve the welcome
screen for the genuinely-no-project case and surface specific diagnostics
for the other failure modes.
"""

from __future__ import annotations

from pathlib import Path

from nauro.store.registry import (
    find_projects_by_name_v2,
    get_store_path,
    get_store_path_v2,
    resolve_project,
    resolve_v2_from_path,
)
from nauro.store.repo_config import (
    RepoConfigSchemaError,
    find_repo_config,
    load_repo_config,
)


class StoreResolutionError(ValueError):
    """Base class for store-resolution failures.

    Subclasses each name a failure category so callers can map them to the
    transport-appropriate output. Inherits ``ValueError`` so callers that
    catch ``ValueError`` from the legacy surface keep working.
    """


class NoProjectError(StoreResolutionError):
    """No project resolvable at all — no ``project_id``, no cwd config, no
    legacy resolution path matched. This is the genuine onboarding case;
    transports should surface the welcome screen pointing the user at
    ``nauro init``.
    """


class ProjectNotFoundError(StoreResolutionError):
    """Caller named a project (by id or name) but no match exists in the
    registry. Distinguished from :class:`NoProjectError` because the
    caller supplied a handle — they have the wrong one, not no handle.
    """


class StoreMissingError(StoreResolutionError):
    """Resolved a ``project_id`` (via cwd config or registry) but its
    store directory does not exist on disk. Usually means ``NAURO_HOME``
    was changed between ``nauro init`` and this call.
    """


class ProjectIdMismatchError(StoreResolutionError):
    """Caller's ``project_id`` does not match the cwd config id. Surface
    the mismatch so the caller can decide whether the cwd or the handle
    is stale.
    """


class MultipleProjectsError(StoreResolutionError):
    """Caller's project name resolves to multiple registry entries. The
    caller must pass an unambiguous ``project_id`` instead.
    """


def resolve_via_repo_config(start: Path | None) -> tuple[str, Path] | None:
    """Walk up from ``start`` looking for ``.nauro/config.json``.

    Returns ``(project_id, store_path)`` or ``None`` when no config is found.
    Mirrors how git locates ``.git`` from anywhere inside a working tree.
    """
    config_path = find_repo_config(start=start)
    if config_path is None:
        return None
    repo_root = config_path.parent.parent
    try:
        cfg = load_repo_config(repo_root)
    except RepoConfigSchemaError:
        return None
    return cfg["id"], get_store_path_v2(cfg["id"])


def resolve_store(project_id: str | None, cwd: str | Path | None) -> Path:
    """Resolve a ``(project_id, cwd)`` pair to a store path.

    Raises a specific :class:`StoreResolutionError` subclass on every
    failure mode so callers can map each one to the appropriate response.
    Callers that just want the welcome screen on any failure can still
    catch the base class (or :class:`ValueError`) and ignore the subtype.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    via_config = resolve_via_repo_config(cwd_path)

    if project_id and via_config is not None:
        config_id, store_path = via_config
        if project_id != config_id:
            matches = find_projects_by_name_v2(project_id)
            if not any(pid == config_id for pid, _ in matches):
                raise ProjectIdMismatchError(
                    f"Supplied project_id {project_id!r} does not match the "
                    f"repo config id {config_id!r} in {cwd_path}."
                )
        if not store_path.exists():
            raise StoreMissingError(
                f"Project store not found for id {config_id!r}. "
                "The cwd .nauro/config.json resolves but the store is "
                "missing from NAURO_HOME — was the home changed since init?"
            )
        return store_path

    if via_config is not None:
        _pid, store_path = via_config
        if not store_path.exists():
            raise StoreMissingError(
                f"Project store not found at {store_path}. The cwd "
                ".nauro/config.json resolves but the store is missing "
                "from NAURO_HOME — was the home changed since init?"
            )
        return store_path

    if project_id:
        matches = find_projects_by_name_v2(project_id)
        if len(matches) == 1:
            pid, _entry = matches[0]
            store_path = get_store_path_v2(pid)
            if not store_path.exists():
                raise StoreMissingError(
                    f"Project store not found for {project_id!r} (id {pid!r}). "
                    "The registry resolves but the store is missing from NAURO_HOME."
                )
            return store_path
        if len(matches) > 1:
            raise MultipleProjectsError(
                f"Multiple v2 projects named {project_id!r}; pass an "
                "unambiguous project_id (ULID) instead of the name."
            )
        # v1 legacy fallback.
        store_path = get_store_path(project_id)
        if not store_path.exists():
            raise ProjectNotFoundError(
                f"No project named or keyed {project_id!r} found in the "
                "registry. Run 'nauro init <name>' to create it, or check "
                "NAURO_HOME if you expected an existing project."
            )
        return store_path

    if cwd:
        name = resolve_project(cwd_path)
        if name:
            store_path = get_store_path(name)
            if store_path.exists():
                return store_path
        v2_match = resolve_v2_from_path(cwd_path)
        if v2_match is not None:
            pid, _entry = v2_match
            store_path = get_store_path_v2(pid)
            if store_path.exists():
                return store_path

    raise NoProjectError(
        "No Nauro project found. Run 'nauro init <name>' in the current "
        "directory to create one, or pass 'project_id' / 'cwd' to point at "
        "an existing project."
    )


__all__ = [
    "MultipleProjectsError",
    "NoProjectError",
    "ProjectIdMismatchError",
    "ProjectNotFoundError",
    "StoreMissingError",
    "StoreResolutionError",
    "resolve_store",
    "resolve_via_repo_config",
]
