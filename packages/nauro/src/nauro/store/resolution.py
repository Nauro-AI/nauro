"""Store-resolution helper and typed exceptions.

The local stdio MCP transport translates a ``(project_id, cwd)`` pair into a path
under the active ``NAURO_HOME``. This module owns the resolution rules and
surfaces every failure as a typed exception, so a wrapper can reserve the
``WELCOME_NO_PROJECT`` onboarding screen for the genuinely-no-project case and
give specific diagnostics for the other failure modes.

Resolution order:

  1. cwd's ``.nauro/config.json`` walk-up (id-keyed v2 store).
  2. ``project_id`` matched against the v2 registry by id, then by name.
  3. ``cwd`` matched against the v2 registry by repo path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

import pydantic as pyd
from nauro_core.identifiers import (
    IdentifierKind,
    is_identifier,
    validate_identifier,
)

from nauro.constants import (
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_SCHEMA_VERSION,
)
from nauro.onboarding import disconnected_project_guidance
from nauro.store.home import registry_file
from nauro.store.registry import (
    RegistryEntryV2,
    StoreBindingError,
    find_projects_by_name_v2,
    get_project_entry_v2,
    get_project_v2,
    get_store_path_v2,
    registered_store_path_hint_v2,
    resolve_registered_store_path_v2,
    resolve_v2_from_path,
    validate_registry_entry_v2,
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

    ``project_id`` is the store key the sync layer pulls under: a ULID from the repo
    config or the v2 registry. ``display_name`` is for CLI output. ``store_path`` is
    not existence-checked, so each caller decides how to treat a missing store.
    """

    store_path: Path
    project_id: str
    display_name: str


@dataclass(frozen=True)
class ResolvedProjectBinding:
    """A validated local or cloud project binding."""

    store_path: Path
    project_id: str
    display_name: str
    mode: Literal["local", "cloud"]
    server_url: str | None


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


def _canonical_project_name(value: str) -> str:
    if (
        value != value.strip()
        or not value
        or len(value) > 100
        or "/" in value
        or "\\" in value
        or ".." in value
        or any(not char.isprintable() for char in value)
    ):
        raise ValueError("project name is not canonical")
    return value


def _canonical_project_id(value: str) -> str:
    return validate_identifier(IdentifierKind.ulid, value, field="project_id")


_CanonicalProjectName = Annotated[pyd.StrictStr, pyd.AfterValidator(_canonical_project_name)]
_CanonicalProjectId = Annotated[pyd.StrictStr, pyd.AfterValidator(_canonical_project_id)]
_RegistrySchemaVersion = Annotated[
    pyd.StrictInt,
    pyd.Field(ge=REGISTRY_SCHEMA_VERSION_V2, le=REGISTRY_SCHEMA_VERSION_V2),
]
_RepoConfigSchemaVersion = Annotated[
    pyd.StrictInt,
    pyd.Field(ge=REPO_CONFIG_SCHEMA_VERSION, le=REPO_CONFIG_SCHEMA_VERSION),
]
_STRICT_MODEL_CONFIG = pyd.ConfigDict(extra="forbid", frozen=True, strict=True)


class _StrictRegistryEntry(RegistryEntryV2):
    model_config = _STRICT_MODEL_CONFIG

    name: _CanonicalProjectName

    @pyd.field_validator("repo_paths")
    @classmethod
    def _validate_repo_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            candidate = Path(value)
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValueError("repo path is not canonical") from exc
            if (
                not value
                or not candidate.is_absolute()
                or str(candidate) != value
                or resolved != candidate
            ):
                raise ValueError("repo path is not canonical")
        return values


class _StrictRegistrySnapshot(pyd.BaseModel):
    model_config = _STRICT_MODEL_CONFIG

    schema_version: _RegistrySchemaVersion
    projects: dict[_CanonicalProjectId, _StrictRegistryEntry]

    @pyd.model_validator(mode="after")
    def _validate_project_map(self) -> _StrictRegistrySnapshot:
        path_owners: dict[str, str] = {}
        for project_id, entry in self.projects.items():
            for repo_path in entry.repo_paths:
                owner = path_owners.setdefault(repo_path, project_id)
                if owner != project_id:
                    raise ValueError("registry repo path has multiple project owners")
        return self


class _StrictRepoConfig(pyd.BaseModel):
    model_config = _STRICT_MODEL_CONFIG

    schema_version: _RepoConfigSchemaVersion
    mode: Literal["local", "cloud"]
    id: _CanonicalProjectId
    name: _CanonicalProjectName
    server_url: pyd.StrictStr | None = None

    @pyd.model_validator(mode="after")
    def _validate_cloud_server(self) -> _StrictRepoConfig:
        if self.mode == "cloud" and not (self.server_url or "").strip():
            raise ValueError("cloud repo config requires a nonempty server URL")
        return self


def _strict_registry_entries(
    target_id: str | None,
    target_cfg: _StrictRepoConfig | None,
) -> _StrictRegistrySnapshot:
    path = registry_file()
    if not path.exists():
        return _StrictRegistrySnapshot(schema_version=REGISTRY_SCHEMA_VERSION_V2, projects={})
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StoreResolutionError("Registry is malformed or unreadable.") from exc
    try:
        return _StrictRegistrySnapshot.model_validate_json(raw)
    except pyd.ValidationError as exc:
        errors = exc.errors(include_input=False)
        if (
            target_id is not None
            and bool(errors)
            and all(tuple(error["loc"][:2]) == ("projects", target_id) for error in errors)
            and is_identifier(IdentifierKind.ulid, target_id)
        ):
            cfg = target_cfg or _StrictRepoConfig(
                schema_version=REPO_CONFIG_SCHEMA_VERSION,
                id=target_id,
                name=target_id,
                mode="local",
            )
            raise DisconnectedProjectError(
                _disconnected(cfg, "connected_record_invalid", get_store_path_v2(target_id))
            ) from exc
        if any(error["type"] == "json_invalid" for error in errors):
            message = "Registry is malformed or unreadable."
        elif any(error["loc"] and error["loc"][0] == "schema_version" for error in errors):
            message = "Registry has an invalid schema."
        elif any(tuple(error["loc"]) == ("projects",) for error in errors):
            message = "Registry has an invalid projects map."
        elif any(error["loc"] and error["loc"][0] == "projects" for error in errors):
            message = "Registry entry is invalid."
        else:
            message = "Registry must be a valid JSON object."
        raise StoreResolutionError(message) from exc


def _strict_repo_config_from_cwd(start: Path) -> _StrictRepoConfig | None:
    config_path = find_repo_config(start=start)
    if config_path is None:
        return None
    repo_root = config_path.parent.parent
    refusal = find_symlink(repo_root, ".nauro/config.json")
    if refusal is not None:
        raise StoreResolutionError(refusal.message)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StoreResolutionError(f"Repo config at {config_path} is invalid.") from exc
    try:
        return _StrictRepoConfig.model_validate_json(raw)
    except pyd.ValidationError as exc:
        fields = {error["loc"][0] for error in exc.errors(include_input=False) if error["loc"]}
        if "schema_version" in fields:
            message = f"Repo config at {config_path} has an invalid schema."
        elif fields.intersection({"id", "name"}):
            message = f"Repo config at {config_path} has an invalid identity."
        else:
            message = f"Repo config at {config_path} is invalid."
        raise StoreResolutionError(message) from exc


def _resolved_binding(
    project_id: str,
    entry: _StrictRegistryEntry,
    cfg: _StrictRepoConfig | None = None,
) -> ResolvedProjectBinding:
    if cfg is None:
        cfg = _StrictRepoConfig(
            schema_version=REPO_CONFIG_SCHEMA_VERSION,
            id=project_id,
            name=entry.name,
            mode=entry.mode,
            server_url=entry.server_url,
        )
    configured_server = cfg.server_url
    conflict = (
        entry.name != cfg.name
        or entry.mode != cfg.mode
        or (
            entry.mode == "local"
            and (configured_server is not None or entry.server_url is not None)
        )
        or (entry.mode == "cloud" and entry.server_url != configured_server)
    )
    if conflict:
        raise DisconnectedProjectError(
            _disconnected(
                cfg,
                "connected_binding_conflict",
                _store_path_hint(entry, project_id),
            )
        )
    try:
        store_path = resolve_registered_store_path_v2(project_id, registry_entry=entry)
    except StoreBindingError as exc:
        raise DisconnectedProjectError(
            _disconnected(cfg, exc.reason_code, _store_path_hint(entry, project_id))
        ) from exc
    return ResolvedProjectBinding(
        store_path=store_path,
        project_id=project_id,
        display_name=cfg.name,
        mode=entry.mode,
        server_url=entry.server_url,
    )


def _resolve_repo_config_from_cwd(start: Path | None) -> tuple[dict, Path] | None:
    """Walk up from ``start`` for ``.nauro/config.json`` and return ``(config, repo_root)``.
    Returns ``None`` for a missing config, on ``RepoConfigSchemaError`` or ``OSError`` from
    the config read, and for a symlinked ``.nauro`` or ``config.json``, whose refusal is logged.
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
    cfg: dict | _StrictRepoConfig,
    reason_code: DisconnectedReason,
    store_path: Path,
) -> DisconnectedProject:
    if isinstance(cfg, _StrictRepoConfig):
        mode = cfg.mode
        project_id = cfg.id
        display_name = cfg.name
    else:
        mode = cfg["mode"]
        project_id = cfg["id"]
        display_name = cfg.get("name") or project_id
    return DisconnectedProject(
        store_path=store_path,
        project_id=project_id,
        display_name=display_name,
        mode=mode,
        reason_code=reason_code,
        recovery_actions=_recovery_actions(mode, reason_code),
        guidance=disconnected_project_guidance(reason_code, mode),
    )


def _store_path_hint(entry: RegistryEntryV2, project_id: str) -> Path:
    return registered_store_path_hint_v2(project_id, entry) or get_store_path_v2(project_id)


def _resolve_validated_entry(
    cfg: dict,
    project_id: str,
    entry: RegistryEntryV2,
) -> RepoResolution | DisconnectedProject:
    """Shared tail of both connection classifiers: resolve or map to disconnected."""
    try:
        store_path = resolve_registered_store_path_v2(project_id, registry_entry=entry)
    except StoreBindingError as exc:
        return _disconnected(cfg, exc.reason_code, _store_path_hint(entry, project_id))
    return RepoResolution(store_path, project_id, cfg.get("name") or project_id)


def _connection_for_config(cfg: dict) -> RepoResolution | DisconnectedProject:
    project_id = cfg["id"]
    try:
        entry = get_project_entry_v2(project_id)
    except StoreBindingError:
        return _disconnected(
            cfg,
            "connected_record_invalid",
            get_store_path_v2(project_id),
        )
    if entry is None:
        return _disconnected(
            cfg,
            "not_connected_on_this_machine",
            get_store_path_v2(project_id),
        )
    configured_server = cfg.get("server_url")
    if (
        entry.name != cfg.get("name")
        or entry.mode != cfg.get("mode")
        or (cfg.get("mode") == "cloud" and entry.server_url != configured_server)
    ):
        return _disconnected(
            cfg,
            "connected_binding_conflict",
            _store_path_hint(entry, project_id),
        )
    return _resolve_validated_entry(cfg, project_id, entry)


def _connection_for_registry_entry(
    project_id: str,
    raw_entry: object,
) -> RepoResolution | DisconnectedProject:
    try:
        entry = validate_registry_entry_v2(project_id, raw_entry)
    except StoreBindingError:
        cfg = {"id": project_id, "name": project_id, "mode": "local"}
        return _disconnected(
            cfg,
            "connected_record_invalid",
            get_store_path_v2(project_id),
        )
    cfg = {
        "id": project_id,
        "name": entry.name,
        "mode": entry.mode,
    }
    if entry.server_url:
        cfg["server_url"] = entry.server_url
    return _resolve_validated_entry(cfg, project_id, entry)


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

    Returns ``(project_id, store_path)``, or ``None`` when no config is found.
    """
    resolved = _resolve_repo_config_from_cwd(start)
    if resolved is None:
        return None
    cfg, _repo_root = resolved
    connection = _connection_for_config(cfg)
    return cfg["id"], connection.store_path


def resolve_from_cwd(cwd: str | Path | None) -> RepoResolution | DisconnectedProject | None:
    """Resolve a cwd to a project store through the two cwd-based tiers in order.

    ``None`` means no project; a missing store comes back as ``DisconnectedProject``.
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

    return None


def _store_path_or_raise(connection: RepoResolution | DisconnectedProject) -> Path:
    """Unwrap a connection result, raising the typed error for disconnected states."""
    if isinstance(connection, DisconnectedProject):
        raise DisconnectedProjectError(connection)
    return connection.store_path


def resolve_project_binding(
    project_id: str | None,
    cwd: str | Path | None,
) -> ResolvedProjectBinding:
    """Resolve and validate a local or cloud project binding."""
    cwd_path = Path(cwd) if cwd else Path.cwd()
    cfg = _strict_repo_config_from_cwd(cwd_path)
    target_id = cfg.id if cfg is not None else project_id
    entries = _strict_registry_entries(target_id, cfg).projects

    if cfg is not None:
        config_id = cfg.id
        entry = entries.get(config_id)
        if (
            project_id
            and project_id != config_id
            and (project_id in entries or entry is None or entry.name != project_id)
        ):
            raise ProjectIdMismatchError(
                f"Supplied project_id {project_id!r} does not match the "
                f"repo config id {config_id!r} in {cwd_path}."
            )
        if entry is None:
            raise DisconnectedProjectError(
                _disconnected(
                    cfg,
                    "not_connected_on_this_machine",
                    get_store_path_v2(config_id),
                )
            )
        return _resolved_binding(config_id, entry, cfg)

    if project_id:
        entry = entries.get(project_id)
        if entry is not None:
            return _resolved_binding(project_id, entry)
        name_matches = [(pid, entry) for pid, entry in entries.items() if entry.name == project_id]
        if len(name_matches) == 1:
            return _resolved_binding(*name_matches[0])
        if len(name_matches) > 1:
            raise MultipleProjectsError(
                f"Multiple v2 projects named {project_id!r}; pass an "
                "unambiguous project_id (ULID) instead of the name."
            )
        raise ProjectNotFoundError(
            f"No project named or keyed {project_id!r} found in the "
            "registry. Run 'nauro init <name>' to create it, or check "
            "NAURO_HOME if you expected an existing project."
        )

    if cwd:
        resolved_cwd = cwd_path.resolve()
        path_matches = {
            pid: entry
            for pid, entry in entries.items()
            if any(
                resolved_cwd == Path(repo_path) or Path(repo_path) in resolved_cwd.parents
                for repo_path in entry.repo_paths
            )
        }
        if len(path_matches) == 1:
            pid, entry = next(iter(path_matches.items()))
            return _resolved_binding(pid, entry)
        if len(path_matches) > 1:
            raise MultipleProjectsError(
                f"Multiple v2 projects match cwd {str(cwd_path)!r}; pass an "
                "unambiguous project_id (ULID) instead."
            )

    raise NoProjectError(
        "No Nauro project found. Run 'nauro init <name>' in the current "
        "directory to create one, or pass 'project_id' / 'cwd' to point at "
        "an existing project."
    )


def resolve_store(project_id: str | None, cwd: str | Path | None) -> Path:
    """Resolve a ``(project_id, cwd)`` pair to a store path.

    Every failure mode raises its own :class:`StoreResolutionError` subclass.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    config_resolution = _resolve_repo_config_from_cwd(cwd_path)

    if config_resolution is not None:
        cfg, _repo_root = config_resolution
        config_id = cfg["id"]
        if project_id and project_id != config_id:
            matches = find_projects_by_name_v2(project_id)
            if not any(pid == config_id for pid, _ in matches):
                raise ProjectIdMismatchError(
                    f"Supplied project_id {project_id!r} does not match the "
                    f"repo config id {config_id!r} in {cwd_path}."
                )
        return _store_path_or_raise(_connection_for_config(cfg))

    if project_id:
        connection = resolve_registered_project(project_id)
        if connection is not None:
            return _store_path_or_raise(connection)
        matches = find_projects_by_name_v2(project_id)
        if len(matches) == 1:
            pid, entry = matches[0]
            return _store_path_or_raise(_connection_for_registry_entry(pid, entry))
        if len(matches) > 1:
            raise MultipleProjectsError(
                f"Multiple v2 projects named {project_id!r}; pass an "
                "unambiguous project_id (ULID) instead of the name."
            )
        raise ProjectNotFoundError(
            f"No project named or keyed {project_id!r} found in the "
            "registry. Run 'nauro init <name>' to create it, or check "
            "NAURO_HOME if you expected an existing project."
        )

    if cwd:
        cwd_connection = resolve_from_cwd(cwd_path)
        if cwd_connection is not None:
            return _store_path_or_raise(cwd_connection)

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
    "ResolvedProjectBinding",
    "StoreMissingError",
    "StoreResolutionError",
    "resolve_from_cwd",
    "resolve_project_binding",
    "resolve_registered_project",
    "resolve_store",
    "resolve_via_repo_config",
]
