"""nauro init — Register a new project and scaffold its store.

Two modes:

* ``nauro init <name>`` — local-only project. CLI mints a ULID, writes
  ``.nauro/config.json`` in the cwd, and registers the project in the
  v2 registry under that id. No network calls.
* ``nauro init --cloud <name>`` — cloud-scoped project. The CLI calls
  the remote MCP server's ``POST /projects`` to mint a server-side ULID,
  then registers locally with ``mode=cloud`` and writes a cloud-mode
  repo config.

``--add-repo <path>`` (repeatable) associates an existing local project
with one or more repo paths. Adding repos to a cloud-scoped project is
intentionally rejected — use ``nauro attach <project_id>`` from the new
repo instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from nauro.cli.commands.auth import DEFAULT_API_URL
from nauro.constants import (
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
)
from nauro.store.registry import (
    add_repo_v2,
    find_projects_by_name_v2,
    get_store_path_v2,
    register_project_v2,
)
from nauro.store.repo_config import save_repo_config
from nauro.sync.cloud_projects import CloudProjectError, create_project
from nauro.telemetry import capture
from nauro.telemetry.events import project_created
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
    cloud: bool = typer.Option(
        False,
        "--cloud",
        help="Create a cloud-scoped project on the remote MCP server.",
    ),
) -> None:
    """Initialize a new Nauro project store and register it.

    If a project with the given name already exists locally and --add-repo
    is provided, the repos are appended to the existing local-mode entry.
    Cloud-mode entries cannot be extended this way — use ``nauro attach``.
    """
    repo_paths = add_repo_paths if add_repo_paths else [Path.cwd()]

    # ── --add-repo against an existing project ──────────────────────────────
    if add_repo_paths:
        existing = find_projects_by_name_v2(name)
        if existing:
            if len(existing) > 1:
                typer.echo(
                    f"Multiple projects named '{name}' exist. "
                    "Disambiguate manually in ~/.nauro/registry.json.",
                    err=True,
                )
                raise typer.Exit(code=1)
            pid, entry = existing[0]
            if entry.get("mode") == REPO_CONFIG_MODE_CLOUD:
                typer.echo(
                    f"Cannot --add-repo to cloud-mode project '{name}'.\n"
                    f"  Run from the new repo: nauro attach {pid}",
                    err=True,
                )
                raise typer.Exit(code=1)
            store_path = get_store_path_v2(pid)
            added = []
            for rp in repo_paths:
                add_repo_v2(pid, rp)
                added.append(rp.resolve())
            typer.echo(f"Updated project '{name}'")
            typer.echo(f"  Store: {store_path}")
            for rp in added:
                typer.echo(f"  Added repo: {rp}")
            return

    # ── New project: cloud or local ────────────────────────────────────────
    if cloud:
        try:
            view = create_project(name)
        except CloudProjectError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)
        try:
            pid, store_path = register_project_v2(
                name,
                repo_paths,
                mode=REPO_CONFIG_MODE_CLOUD,
                project_id=view["project_id"],
                server_url=DEFAULT_API_URL,
            )
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)
        capture("project.created", project_created(REGISTRY_SCHEMA_VERSION_V2))
        for rp in repo_paths:
            save_repo_config(
                rp,
                {
                    "mode": REPO_CONFIG_MODE_CLOUD,
                    "id": pid,
                    "name": name,
                    "server_url": DEFAULT_API_URL,
                },
            )
        scaffold_project_store(name, store_path)
        typer.echo(f"Initialized cloud project '{name}'")
        typer.echo(f"  Project id: {pid}")
        typer.echo(f"  Store: {store_path}")
        for rp in repo_paths:
            typer.echo(f"  Repo:  {rp.resolve()}")
        typer.echo("  Next: run 'nauro sync' to capture the first snapshot")
        return

    # ── Local-only ─────────────────────────────────────────────────────────
    try:
        pid, store_path = register_project_v2(
            name,
            repo_paths,
            mode=REPO_CONFIG_MODE_LOCAL,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    capture("project.created", project_created(REGISTRY_SCHEMA_VERSION_V2))
    for rp in repo_paths:
        save_repo_config(
            rp,
            {
                "mode": REPO_CONFIG_MODE_LOCAL,
                "id": pid,
                "name": name,
            },
        )

    if demo:
        from nauro.demo import create_demo_project

        create_demo_project(store_path)
        typer.echo(f"Initialized demo project '{name}'")
        typer.echo(f"  Project id: {pid}")
        typer.echo(f"  Store: {store_path}")
        typer.echo("  Includes: 7 decisions, project state, open questions, and a snapshot")

        _try_demo_sync(name, store_path)
    else:
        scaffold_project_store(name, store_path)
        typer.echo(f"Initialized project '{name}'")
        typer.echo(f"  Project id: {pid}")
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
