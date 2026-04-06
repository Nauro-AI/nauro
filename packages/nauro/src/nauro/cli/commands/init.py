"""nauro init — Register a new project and scaffold its store."""

import logging
from pathlib import Path

import typer

from nauro.store.registry import add_repo, get_project, get_store_path, register_project
from nauro.templates.scaffolds import scaffold_project_store

logger = logging.getLogger("nauro.cli.init")


def init(
    name: str = typer.Argument(default="demo-project", help="Project name."),
    add_repo_paths: list[Path] | None = typer.Option(
        None,
        "--add-repo",
        help="Repo directory to associate (can be repeated). Defaults to cwd.",
    ),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Create a sample project with pre-written decisions.",
    ),
) -> None:
    """Initialize a new Nauro project store and register it.

    If the project already exists and --add-repo is provided, adds the repo
    paths to the existing project instead of failing.
    """
    repo_paths = add_repo_paths if add_repo_paths else [Path.cwd()]

    # If project exists and --add-repo was explicitly provided, add repos
    if get_project(name) is not None and add_repo_paths:
        store_path = get_store_path(name)
        added = []
        for rp in repo_paths:
            add_repo(name, rp)
            added.append(rp.resolve())
        typer.echo(f"Updated project '{name}'")
        typer.echo(f"  Store: {store_path}")
        for rp in added:
            typer.echo(f"  Added repo: {rp}")
        return

    try:
        store_path = register_project(name, repo_paths)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)

    if demo:
        from nauro.demo import create_demo_project

        create_demo_project(store_path)
        typer.echo(f"Initialized demo project '{name}'")
        typer.echo(f"  Store: {store_path}")
        typer.echo("  Includes: 3 decisions, project state, open questions, and a snapshot")

        # Auto-push if sync is configured
        _try_demo_sync(name, store_path)
    else:
        scaffold_project_store(name, store_path)
        typer.echo(f"Initialized project '{name}'")
        typer.echo(f"  Store: {store_path}")
        for rp in repo_paths:
            typer.echo(f"  Repo:  {rp.resolve()}")
        typer.echo("  Next: run 'nauro sync' to capture the first snapshot")


def _try_demo_sync(project_name: str, store_path: Path) -> None:
    """Best-effort push demo project to S3 if auth is configured."""
    try:
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        if not config.enabled or not (config.user_id or config.sanitized_sub):
            return

        from nauro.sync.hooks import push_after_write

        push_after_write(project_name, store_path)
        typer.echo("  Synced to remote")
    except Exception:
        logger.debug("Demo sync failed (auth not configured?)", exc_info=True)
