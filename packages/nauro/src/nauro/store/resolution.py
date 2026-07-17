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

The typed subclasses below let the wrappers reserve the
``WELCOME_NO_PROJECT`` onboarding screen for the genuinely-no-project case
and surface specific diagnostics for the other failure modes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from nauro.onboarding import disconnected_project_guidance
from nauro.store.registry import (
    StoreBindingError,
    find_projects_by_name_v2,
    get_project_v2,
    get_store_path,
    get_store_path_v2,
    registered_store_path_hint_v2,
    resolve_project,
    resolve_registered_store_path_v2,
    resolve_v2_from_path,
)
from nauro.store.repo_config import (
    RepoConfigSchemaError,
    find_repo_config,
    load_repo_config,
)
from nauro.store.write_safety import find_symlink

logger = logging.getLogger("nauro.resolution")


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


class DisconnectedProjectError(StoreResolutionError):
    """A repository identifies a project whose record is unavailable."""

    def __init__(self, state: DisconnectedProject) -> None:
        super().__init__(state.guidance)
        self.state = state


class ProjectIdMismatchError(StoreResolutionError):
    """Caller's ``project_id`` does not match the cwd config id. Surface
    the mismatch so the caller can decide whether the cwd or the handle
    is stale.
    """


class MultipleProjectsError(StoreResolutionError):
    """Caller's project name resolves to multiple registry entries. The
    caller must pass an unambiguous ``project_id`` instead.
    """


class RepoResolution(NamedTuple):
    """A cwd resolved to a project store.

    ``project_id`` is the store key: a ULID for v2 projects (from the repo
    config or the v2 registry) or the legacy name for v1 projects. It is the
    key the sync layer pulls under. ``display_name`` is the human-facing name
    for CLI output. ``store_path`` is not existence-checked — each caller
    decides how to treat a resolved-but-missing store.
    """

    store_path: Path
    project_id: str
    display_name: str


DisconnectedReason = Literal[
    "not_connected_on_this_machine",
    "connected_record_missing",
    "connected_record_invalid",
    "connected_binding_conflict",
]
RecoveryAction = Literal["locate", "restore", "continue"]


@dataclass(frozen=True)
class DisconnectedProject:
    """Typed negative result for a repository with valid project identity."""

    store_path: Path
    project_id: str
    display_name: str
    mode: str
    reason_code: DisconnectedReason
    recovery_actions: tuple[RecoveryAction, ...]
    guidance: str


def _resolve_repo_config_from_cwd(start: Path | None) -> tuple[dict, Path] | None:
    """Walk up from ``start`` for ``.nauro/config.json`` and load it.

    Returns ``(config, repo_root)`` or ``None`` when no config is found or the
    config is unreadable. Both ``RepoConfigSchemaError`` (schema mismatch, or a
    corrupt-JSON error the reader remaps to it) and ``OSError`` (an unreadable
    file) degrade to ``None`` so a resolution failure surfaces the no-project
    fallback rather than crashing the transport. A config path that traverses
    a symlink (a symlinked ``.nauro`` directory or ``config.json``) degrades
    to ``None`` the same way: a cloned repo is untrusted content, and a
    pre-planted link must not let attacker-chosen content select which
    project a command operates on.
    """
    config_path = find_repo_config(start=start)
    if config_path is None:
        return None
    repo_root = config_path.parent.parent
    refusal = find_symlink(repo_root, ".nauro/config.json")
    if refusal is not None:
        logger.warning("Declining repo config at %s: %s", config_path, refusal.message)
        return None
    try:
        cfg = load_repo_config(repo_root)
    except (RepoConfigSchemaError, OSError):
        return None
    return cfg, repo_root


def _recovery_actions(
    mode: str,
    reason_code: DisconnectedReason,
) -> tuple[RecoveryAction, ...]:
    if mode == "cloud" and reason_code in {
        "not_connected_on_this_machine",
        "connected_record_missing",
    }:
        return ("locate", "restore", "continue")
    return ("locate", "continue")


def _disconnected(
    cfg: dict,
    reason_code: DisconnectedReason,
    store_path: Path,
) -> DisconnectedProject:
    mode = cfg["mode"]
    return DisconnectedProject(
        store_path=store_path,
        project_id=cfg["id"],
        display_name=cfg.get("name") or cfg["id"],
        mode=mode,
        reason_code=reason_code,
        recovery_actions=_recovery_actions(mode, reason_code),
        guidance=disconnected_project_guidance(reason_code, mode),
    )


def _store_path_hint(entry: dict, project_id: str) -> Path:
    return registered_store_path_hint_v2(project_id, entry) or get_store_path_v2(project_id)


def _connection_for_config(cfg: dict) -> RepoResolution | DisconnectedProject:
    project_id = cfg["id"]
    entry = get_project_v2(project_id)
    if entry is None:
        return _disconnected(
            cfg,
            "not_connected_on_this_machine",
            get_store_path_v2(project_id),
        )
    configured_server = cfg.get("server_url")
    if (
        entry.get("name") != cfg.get("name")
        or entry.get("mode") != cfg.get("mode")
        or (cfg.get("mode") == "cloud" and entry.get("server_url") != configured_server)
    ):
        return _disconnected(
            cfg,
            "connected_binding_conflict",
            _store_path_hint(entry, project_id),
        )
    try:
        store_path = resolve_registered_store_path_v2(project_id)
    except StoreBindingError as exc:
        return _disconnected(cfg, exc.reason_code, _store_path_hint(entry, project_id))
    return RepoResolution(store_path, project_id, cfg.get("name") or project_id)


def _connection_for_registry_entry(
    project_id: str,
    entry: dict,
) -> RepoResolution | DisconnectedProject:
    cfg = {
        "id": project_id,
        "name": entry.get("name") or project_id,
        "mode": entry.get("mode") or "local",
    }
    if entry.get("server_url"):
        cfg["server_url"] = entry["server_url"]
    try:
        store_path = resolve_registered_store_path_v2(project_id)
    except StoreBindingError as exc:
        return _disconnected(cfg, exc.reason_code, _store_path_hint(entry, project_id))
    return RepoResolution(store_path, project_id, cfg["name"])


def resolve_registered_project(
    project_id: str,
) -> RepoResolution | DisconnectedProject | None:
    """Resolve one v2 registry entry through the shared connection boundary."""
    entry = get_project_v2(project_id)
    if entry is None:
        return None
    return _connection_for_registry_entry(project_id, entry)


def resolve_via_repo_config(start: Path | None) -> tuple[str, Path] | None:
    """Walk up from ``start`` looking for ``.nauro/config.json``.

    Returns ``(project_id, store_path)`` or ``None`` when no config is found.
    Mirrors how git locates ``.git`` from anywhere inside a working tree.
    """
    resolved = _resolve_repo_config_from_cwd(start)
    if resolved is None:
        return None
    cfg, _repo_root = resolved
    connection = _connection_for_config(cfg)
    return cfg["id"], connection.store_path


def resolve_from_cwd(cwd: str | Path | None) -> RepoResolution | DisconnectedProject | None:
    """Resolve a cwd to a project store via the canonical waterfall.

    Applies the three cwd-based tiers in order and returns the first match:

      1. ``.nauro/config.json`` walk-up (id-keyed v2 store).
      2. v2 registry matched by repo path.
      3. v1 ``resolve_project`` (legacy, name-keyed).

    Returns a :class:`RepoResolution`, or ``None`` when no tier matches. Does
    NOT check that ``store_path`` exists — each caller decides how to treat a
    resolved-but-missing store.
    """
    start = Path(cwd) if cwd else Path.cwd()

    resolved = _resolve_repo_config_from_cwd(start)
    if resolved is not None:
        cfg, _repo_root = resolved
        return _connection_for_config(cfg)

    v2_match = resolve_v2_from_path(start)
    if v2_match is not None:
        pid, entry = v2_match
        return _connection_for_registry_entry(pid, entry)

    name = resolve_project(start)
    if name:
        return RepoResolution(get_store_path(name), name, name)

    return None


def resolve_store(project_id: str | None, cwd: str | Path | None) -> Path:
    """Resolve a ``(project_id, cwd)`` pair to a store path.

    Raises a specific :class:`StoreResolutionError` subclass on every
    failure mode so callers can map each one to the appropriate response.
    Callers that just want the welcome screen on any failure can still
    catch the base class (or :class:`ValueError`) and ignore the subtype.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    config_resolution = _resolve_repo_config_from_cwd(cwd_path)

    if project_id and config_resolution is not None:
        cfg, _repo_root = config_resolution
        config_id = cfg["id"]
        if project_id != config_id:
            matches = find_projects_by_name_v2(project_id)
            if not any(pid == config_id for pid, _ in matches):
                raise ProjectIdMismatchError(
                    f"Supplied project_id {project_id!r} does not match the "
                    f"repo config id {config_id!r} in {cwd_path}."
                )
        connection = _connection_for_config(cfg)
        if isinstance(connection, DisconnectedProject):
            raise DisconnectedProjectError(connection)
        return connection.store_path

    if config_resolution is not None:
        cfg, _repo_root = config_resolution
        connection = _connection_for_config(cfg)
        if isinstance(connection, DisconnectedProject):
            raise DisconnectedProjectError(connection)
        return connection.store_path

    if project_id:
        direct_entry = get_project_v2(project_id)
        if direct_entry is not None:
            connection = _connection_for_registry_entry(project_id, direct_entry)
            if isinstance(connection, DisconnectedProject):
                raise DisconnectedProjectError(connection)
            return connection.store_path
        matches = find_projects_by_name_v2(project_id)
        if len(matches) == 1:
            pid, entry = matches[0]
            connection = _connection_for_registry_entry(pid, entry)
            if isinstance(connection, DisconnectedProject):
                raise DisconnectedProjectError(connection)
            return connection.store_path
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
        cwd_connection = resolve_from_cwd(cwd_path)
        if isinstance(cwd_connection, DisconnectedProject):
            raise DisconnectedProjectError(cwd_connection)
        if cwd_connection is not None:
            return cwd_connection.store_path

    raise NoProjectError(
        "No Nauro project found. Run 'nauro init <name>' in the current "
        "directory to create one, or pass 'project_id' / 'cwd' to point at "
        "an existing project."
    )


__all__ = [
    "DisconnectedProject",
    "DisconnectedProjectError",
    "DisconnectedReason",
    "MultipleProjectsError",
    "NoProjectError",
    "ProjectIdMismatchError",
    "ProjectNotFoundError",
    "RepoResolution",
    "StoreMissingError",
    "StoreResolutionError",
    "resolve_from_cwd",
    "resolve_registered_project",
    "resolve_store",
    "resolve_via_repo_config",
]
