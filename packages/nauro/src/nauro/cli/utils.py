"""Shared CLI utilities — project resolution + path helpers.

Resolution priority for any command that needs a project context:

  1. Explicit ``--project <name>`` flag (when the command takes one).
     Looked up against the v2 registry by name; v1 registry is consulted
     as a legacy fallback for unconverted callers and tests.
  2. ``find_repo_config(cwd)`` — walks up from cwd looking for
     ``.nauro/config.json`` and resolves the v2 registry by id.
  3. v1 ``resolve_project(cwd)`` — name-keyed cwd resolution. Legacy
     fallback retained while v1 callers remain in tree.
  4. Error with helpful guidance.
"""

from __future__ import annotations

from pathlib import Path

import typer

from nauro.store.registry import (
    RegistrySchemaError,
    add_repo_v2,
    find_projects_by_name_v2,
    get_project,
    get_project_v2,
    get_store_path,
    load_registry,
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


class DisconnectedProjectExit(typer.Exit):
    """CLI resolution already rendered typed reconnect guidance."""


def refuse_global_config_collision(repo_root: Path) -> None:
    """Abort when ``repo_root``'s ``.nauro/config.json`` is the global config.

    With the default home layout that is exactly the home directory:
    ``~/.nauro/config.json`` holds credentials and user-level settings, and a
    repo config written there replaces them. Commands that take a repo root
    call this before any registry or store mutation. The refusal is
    deliberately independent of ``--force`` — there is no situation where
    overwriting the global config with a project pointer is what the user
    wanted.

    Raises:
        typer.Exit: code 1 when ``repo_root`` collides with the global config.
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
    """Abort when ``repo_root``'s ``.nauro/config.json`` path traverses a symlink.

    A cloned repo is untrusted content: a pre-planted symlink at ``.nauro`` or
    ``.nauro/config.json`` would redirect the registration write outside the
    checkout. Commands that register a repo call this before any registry,
    cloud, or store mutation so a refusal leaves no partial state.
    ``save_repo_config`` enforces the same rule as the last line of defense.

    Raises:
        typer.Exit: code 1 when a symlink component is found.
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
    """Return the sorted union of v1 and v2 project names, blanks removed.

    The empty-string is subtracted from the *combined* set so a v2 entry
    missing its ``name`` field cannot leak a blank token into the
    "Available projects:" listing.
    """
    registry = load_registry()
    v2_names = {e.get("name", "") for e in _v2_registry_or_empty()["projects"].values()}
    v1_names = set(registry["projects"].keys())
    return sorted((v1_names | v2_names) - {""})


def resolve_target_project(project_flag: str | None) -> tuple[str, Path]:
    """Resolve the target project from --project flag or cwd.

    Args:
        project_flag: Explicit project name from --project, or None.

    Returns:
        (project_display_name, store_path) tuple.

    Raises:
        typer.Exit: If no project can be resolved.
    """
    if project_flag is not None:
        # 1a — v2 by name (canonical)
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
            typer.echo(
                f"Multiple projects named '{project_flag}'. Disambiguate by id: {ids}",
                err=True,
            )
            raise typer.Exit(code=1)
        if len(matches) == 1:
            pid, _entry = matches[0]
            connection = resolve_registered_project(pid)
            if isinstance(connection, DisconnectedProject):
                typer.echo(connection.guidance, err=True)
                raise DisconnectedProjectExit(code=1)
            if connection is not None:
                return project_flag, connection.store_path

        # 1b — v1 fallback (legacy tests and unconverted callers)
        entry = get_project(project_flag)
        if entry is not None:
            return project_flag, get_store_path(project_flag)

        available = _available_project_names()
        typer.echo(f"Unknown project '{project_flag}'.", err=True)
        if available:
            typer.echo(f"Available projects: {', '.join(available)}", err=True)
        else:
            typer.echo("No projects registered. Run 'nauro init' first.", err=True)
        raise typer.Exit(code=1)

    # 2 — cwd waterfall: repo config walk-up → v2 registry by path → v1 legacy
    cwd = Path.cwd()
    resolution = resolve_from_cwd(cwd)
    if isinstance(resolution, DisconnectedProject):
        typer.echo(resolution.guidance, err=True)
        raise DisconnectedProjectExit(code=1)
    if resolution is not None:
        return resolution.display_name, resolution.store_path

    # 3 — error
    available = _available_project_names()
    typer.echo("No project found for current directory.", err=True)

    suggestion = suggest_project_for_path(cwd)
    if suggestion:
        typer.echo(
            f"Hint: project '{suggestion}' exists but this path is not registered.",
            err=True,
        )
        typer.echo(
            f"  Run: nauro init {suggestion} --add-repo .",
            err=True,
        )
    elif available:
        typer.echo(f"Available projects: {', '.join(available)}", err=True)
        typer.echo("Use --project <name> to target a specific project.", err=True)
    else:
        typer.echo("Run 'nauro init' first.", err=True)
    raise typer.Exit(code=1)


def _resolve_project_entry(project_name: str, project_key: str) -> dict:
    """Resolve a registry entry that has at least one associated repo path.

    Args:
        project_name: Display name (used for the v1 lookup and error message).
        project_key: v2 project_id (store directory name) for the v2 lookup.

    Returns:
        The resolved registry entry dict, guaranteed to carry ``repo_paths``.

    Raises:
        typer.Exit: code 1 when no entry resolves or the entry has no repos.
    """
    # Try v2 first (id-keyed), then fall back to v1 (name-keyed legacy)
    try:
        entry = get_project_v2(project_key)
    except RegistrySchemaError:
        entry = None
    if entry is None:
        entry = get_project(project_name)

    if entry is None or not entry.get("repo_paths"):
        typer.echo(f"Project '{project_name}' has no associated repos.", err=True)
        raise typer.Exit(code=1)
    return entry


# Re-exported for callers that need to write or extend v2 entries directly
__all__ = [
    "add_repo_v2",
    "DisconnectedProjectExit",
    "refuse_global_config_collision",
    "refuse_repo_config_symlink",
    "register_project_v2",
    "resolve_target_project",
]
