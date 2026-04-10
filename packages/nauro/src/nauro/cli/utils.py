"""Shared CLI utilities."""

from pathlib import Path

import typer

from nauro.store.registry import (
    get_project,
    get_store_path,
    load_registry,
    resolve_project,
    suggest_project_for_path,
)


def resolve_target_project(project_flag: str | None) -> tuple[str, Path]:
    """Resolve the target project from --project flag or cwd.

    Args:
        project_flag: Explicit project name from --project, or None.

    Returns:
        (project_name, store_path) tuple.

    Raises:
        typer.Exit: If no project can be resolved.
    """
    if project_flag is not None:
        entry = get_project(project_flag)
        if entry is None:
            registry = load_registry()
            available = sorted(registry["projects"].keys())
            typer.echo(f"Unknown project '{project_flag}'.", err=True)
            if available:
                typer.echo(f"Available projects: {', '.join(available)}", err=True)
            else:
                typer.echo("No projects registered. Run 'nauro init' first.", err=True)
            raise typer.Exit(code=1)
        return project_flag, get_store_path(project_flag)

    cwd = Path.cwd()
    project_name = resolve_project(cwd)
    if project_name:
        return project_name, get_store_path(project_name)

    registry = load_registry()
    available = sorted(registry["projects"].keys())
    typer.echo("No project found for current directory.", err=True)

    # Check if the directory name matches a project — likely a moved/cloned repo
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
