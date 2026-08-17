"""Shared CLI utilities — project resolution + path helpers.

Resolution priority for any command that needs a project context:

  1. Explicit ``--project <name>`` flag (when the command takes one),
     looked up against the registry by name.
  2. The cwd waterfall: ``.nauro/config.json`` walk-up, then the registry
     matched by repo path.
  3. Error with helpful guidance.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import typer

from nauro import __version__
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.journal import OriginDescriptor
from nauro.store.registry import (
    RegistrySchemaError,
    add_repo_v2,
    find_projects_by_name_v2,
    get_project_v2,
    load_registry_v2,
    register_project_v2,
    suggest_project_for_path,
)
from nauro.store.repo_config import collides_with_global_config
from nauro.store.resolution import (
    DisconnectedProject,
    resolve_from_cwd,
    resolve_registered_project,
)
from nauro.store.write_safety import find_symlink

# Pinned vocabulary for ProjectResolutionExit.reason. Machine-readable
# consumers (e.g. ``nauro status --json``) surface these values verbatim.
RESOLUTION_AMBIGUOUS_PROJECT = "ambiguous_project"
RESOLUTION_UNKNOWN_PROJECT = "unknown_project"
RESOLUTION_DISCONNECTED_PROJECT = "disconnected_project"
RESOLUTION_NO_PROJECT = "no_project"


class ProjectResolutionExit(typer.Exit):
    """Project resolution failed; the guidance was already printed to stderr.

    ``reason`` is one of the pinned vocabulary values above; ``guidance`` is
    the exact message the raise site printed, so structured consumers can
    re-surface it without re-deriving the prose.
    """

    def __init__(self, reason: str, guidance: str) -> None:
        super().__init__(code=1)
        self.reason = reason
        self.guidance = guidance


class DisconnectedProjectExit(ProjectResolutionExit):
    """CLI resolution already rendered typed reconnect guidance."""

    def __init__(self, guidance: str) -> None:
        super().__init__(RESOLUTION_DISCONNECTED_PROJECT, guidance)


def cli_origin() -> OriginDescriptor | None:
    """Return the origin descriptor stamped on every write-path event from the CLI surface.
    Total by construction: origin is provenance, never load-bearing for the write, so any
    failure yields ``None`` rather than raising.
    """
    try:
        return OriginDescriptor(
            transport="cli",
            client_name="nauro-cli",
            client_version=__version__,
        )
    except Exception:
        return None


def refuse_global_config_collision(repo_root: Path) -> None:
    """Abort with ``typer.Exit`` 1 when ``repo_root``'s ``.nauro/config.json`` is the global one.
    Callers taking a repo root call this before any registry or store mutation. The refusal is
    independent of ``--force``: a project pointer must never replace the user's credentials.
    """
    if not collides_with_global_config(repo_root):
        return
    typer.echo(
        f"Cannot use {repo_root.resolve()} as a project directory: its "
        ".nauro/config.json is Nauro's own global config file, which holds "
        "credentials and user-level settings.\n"
        "Run this command from a project directory instead, e.g.:\n"
        "  mkdir my-project && cd my-project",
        err=True,
    )
    raise typer.Exit(code=1)


def refuse_repo_config_symlink(repo_root: Path) -> None:
    """Abort with ``typer.Exit`` 1 when ``.nauro/config.json`` traverses a symlink.
    Called before any registry, cloud or store mutation so a refusal leaves no partial state.
    ``save_repo_config`` enforces the same rule as the last line of defense.
    """
    refusal = find_symlink(repo_root, ".nauro/config.json")
    if refusal is None:
        return
    typer.echo(f"Error: {refusal.message}", err=True)
    raise typer.Exit(code=1)


def _v2_registry_or_empty() -> dict:
    """Return the v2 registry; on schema error fall back to an empty shape.

    The schema-mismatch error is surfaced separately during resolution so
    each command shows a single coherent message. Helper sites that just
    need the registry data (e.g. iterating) get an empty shape.
    """
    try:
        return load_registry_v2()
    except RegistrySchemaError:
        return {"projects": {}}


def _available_project_names() -> list[str]:
    """Return the sorted project names, blanks removed.

    The empty string is subtracted so an entry missing its ``name`` field
    cannot leak a blank token into the "Available projects:" listing.
    """
    names = {e.get("name", "") for e in _v2_registry_or_empty()["projects"].values()}
    return sorted(names - {""})


def resolve_target_project(project_flag: str | None) -> tuple[str, Path]:
    """Resolve the target project from ``--project`` or the cwd, as ``(display_name, store_path)``.
    Raises ``ProjectResolutionExit`` carrying the pinned reason and the printed guidance when
    nothing resolves.
    """
    if project_flag is not None:
        # 1 — registry lookup by name
        matches = find_projects_by_name_v2(project_flag)
        if len(matches) > 1:
            # Show the full ULID, not a prefix: ULIDs minted seconds apart share
            # a long time-based prefix, so a short slice can render identically
            # for every match and is not accepted as a --project value anyway.
            ids = ", ".join(
                f"{name} ({pid})"
                for pid, _entry in matches[:5]
                for name in [_entry.get("name", "?")]
            )
            message = f"Multiple projects named '{project_flag}'. Disambiguate by id: {ids}"
            typer.echo(message, err=True)
            raise ProjectResolutionExit(RESOLUTION_AMBIGUOUS_PROJECT, message)
        if len(matches) == 1:
            pid, _entry = matches[0]
            connection = resolve_registered_project(pid)
            if isinstance(connection, DisconnectedProject):
                typer.echo(connection.guidance, err=True)
                raise DisconnectedProjectExit(connection.guidance)
            if connection is not None:
                return project_flag, connection.store_path

        available = _available_project_names()
        lines = [f"Unknown project '{project_flag}'."]
        if available:
            lines.append(f"Available projects: {', '.join(available)}")
        else:
            lines.append("No projects registered. Run 'nauro init' first.")
        message = "\n".join(lines)
        typer.echo(message, err=True)
        raise ProjectResolutionExit(RESOLUTION_UNKNOWN_PROJECT, message)

    # 2 — cwd waterfall: repo config walk-up → registry by repo path
    cwd = Path.cwd()
    resolution = resolve_from_cwd(cwd)
    if isinstance(resolution, DisconnectedProject):
        typer.echo(resolution.guidance, err=True)
        raise DisconnectedProjectExit(resolution.guidance)
    if resolution is not None:
        return resolution.display_name, resolution.store_path

    # 3 — error
    available = _available_project_names()
    lines = ["No project found for current directory."]

    suggestion = suggest_project_for_path(cwd)
    if suggestion:
        pid, entry = suggestion
        name = entry.get("name", "")
        lines.append(f"Hint: project {name!r} exists but this path is not registered.")
        # init --add-repo intentionally rejects cloud-scoped projects; the
        # documented association path for those is nauro attach. The name is
        # shell-quoted because the line is meant to be copy-pasted.
        if entry.get("mode") == REPO_CONFIG_MODE_CLOUD:
            lines.append(f"  Run: nauro attach {pid}")
        else:
            lines.append(f"  Run: nauro init {shlex.quote(name)} --add-repo .")
    elif available:
        lines.append(f"Available projects: {', '.join(available)}")
        lines.append("Use --project <name> to target a specific project.")
    else:
        lines.append("Run 'nauro init' first.")
    message = "\n".join(lines)
    typer.echo(message, err=True)
    raise ProjectResolutionExit(RESOLUTION_NO_PROJECT, message)


def _resolve_project_entry(project_name: str, project_key: str) -> dict:
    """Resolve a registry entry that has at least one associated repo path.

    Args:
        project_name: Display name (used in the error message).
        project_key: project_id (store directory name) for the lookup.

    Returns:
        The resolved registry entry dict, guaranteed to carry ``repo_paths``.

    Raises:
        typer.Exit: code 1 when no entry resolves or the entry has no repos.
    """
    entry = get_project_v2(project_key)
    if entry is None or not entry.get("repo_paths"):
        typer.echo(f"Project '{project_name}' has no associated repos.", err=True)
        raise typer.Exit(code=1)
    return entry


# Re-exported for callers that need to write or extend v2 entries directly
__all__ = [
    "add_repo_v2",
    "cli_origin",
    "DisconnectedProjectExit",
    "ProjectResolutionExit",
    "refuse_global_config_collision",
    "refuse_repo_config_symlink",
    "register_project_v2",
    "resolve_target_project",
]
