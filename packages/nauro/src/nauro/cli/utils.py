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

import json
from pathlib import Path

import typer

from nauro.store.registry import (
    RegistrySchemaError,
    add_repo_v2,
    find_projects_by_name_v2,
    get_project,
    get_store_path,
    get_store_path_v2,
    load_registry,
    load_registry_v2,
    register_project_v2,
    resolve_project,
    resolve_v2_from_path,
    suggest_project_for_path,
)
from nauro.store.repo_config import (
    RepoConfigSchemaError,
    find_repo_config,
    load_repo_config,
)


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


def _resolve_from_repo_config() -> tuple[str, Path] | None:
    """Resolve via ``.nauro/config.json`` walk-up from cwd.

    Returns (display_name, store_path) or None when no repo config is found.
    A repo-config that names a project_id missing from the v2 registry is
    still honored — the store path uses the id from the config.
    """
    config_path = find_repo_config()
    if config_path is None:
        return None
    repo_root = config_path.parent.parent
    try:
        cfg = load_repo_config(repo_root)
    except (RepoConfigSchemaError, json.JSONDecodeError, OSError):
        return None
    pid = cfg["id"]
    name = cfg.get("name") or pid
    return name, get_store_path_v2(pid)


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
            ids = ", ".join(
                f"{name} ({pid[:8]})"
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
            return project_flag, get_store_path_v2(pid)

        # 1b — v1 fallback (legacy tests and unconverted callers)
        entry = get_project(project_flag)
        if entry is not None:
            return project_flag, get_store_path(project_flag)

        registry = load_registry()
        v2_names = sorted({e.get("name", "") for e in _v2_registry_or_empty()["projects"].values()})
        v1_names = sorted(registry["projects"].keys())
        available = sorted(set(v1_names) | set(v2_names) - {""})
        typer.echo(f"Unknown project '{project_flag}'.", err=True)
        if available:
            typer.echo(f"Available projects: {', '.join(available)}", err=True)
        else:
            typer.echo("No projects registered. Run 'nauro init' first.", err=True)
        raise typer.Exit(code=1)

    # 2 — repo config walk-up
    via_repo = _resolve_from_repo_config()
    if via_repo is not None:
        return via_repo

    # 2b — v2 registry by cwd repo_paths (no repo config, but registered)
    cwd = Path.cwd()
    v2_match = resolve_v2_from_path(cwd)
    if v2_match is not None:
        pid, entry = v2_match
        return entry.get("name", pid), get_store_path_v2(pid)

    # 3 — v1 fallback (legacy)
    project_name = resolve_project(cwd)
    if project_name:
        return project_name, get_store_path(project_name)

    # 4 — error
    registry = load_registry()
    v2_names = sorted({e.get("name", "") for e in _v2_registry_or_empty()["projects"].values()})
    v1_names = sorted(registry["projects"].keys())
    available = sorted(set(v1_names) | set(v2_names) - {""})
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


# Re-exported for callers that need to write or extend v2 entries directly
__all__ = [
    "add_repo_v2",
    "register_project_v2",
    "resolve_target_project",
]
