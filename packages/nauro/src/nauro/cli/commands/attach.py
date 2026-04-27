"""nauro attach — Associate the current repo with an existing cloud project.

The cloud equivalent of ``nauro init --add-repo``: the project already
exists on the server (someone else created it, or you created it from a
different repo), and you want this repo to participate in its context.

Membership is verified against ``GET /projects`` before any local state
is written. The local store directory is created if it does not yet
exist; a ``.nauro/config.json`` is written into the cwd in cloud mode.
"""

from __future__ import annotations

from pathlib import Path

import typer

from nauro.cli.commands.auth import DEFAULT_API_URL
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.registry import (
    add_repo_v2,
    get_project_v2,
    register_project_v2,
)
from nauro.store.repo_config import save_repo_config
from nauro.sync.cloud_projects import CloudProjectError, list_projects


def attach(
    project_id: str = typer.Argument(..., help="Cloud project_id (ULID)."),
    repo_path: Path | None = typer.Option(
        None,
        "--repo",
        help="Repo directory to attach. Defaults to cwd.",
    ),
) -> None:
    """Attach the current repo to an existing cloud project."""
    repo_path = repo_path if repo_path is not None else Path.cwd()
    try:
        projects = list_projects()
    except CloudProjectError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    match = next((p for p in projects if p["project_id"] == project_id), None)
    if match is None:
        typer.echo(
            f"Project id {project_id!r} not found among your cloud projects.\n"
            "  Check the id, or run 'nauro auth login' to refresh credentials.",
            err=True,
        )
        raise typer.Exit(code=1)

    name = match["name"]

    existing = get_project_v2(project_id)
    if existing is None:
        _pid, store_path = register_project_v2(
            name,
            [repo_path],
            mode=REPO_CONFIG_MODE_CLOUD,
            project_id=project_id,
            server_url=DEFAULT_API_URL,
        )
    else:
        add_repo_v2(project_id, repo_path)
        from nauro.store.registry import get_store_path_v2

        store_path = get_store_path_v2(project_id)

    save_repo_config(
        repo_path,
        {
            "mode": REPO_CONFIG_MODE_CLOUD,
            "id": project_id,
            "name": name,
            "server_url": DEFAULT_API_URL,
        },
    )

    typer.echo(f"Attached '{name}' to {repo_path.resolve()}")
    typer.echo(f"  Project id: {project_id}")
    typer.echo(f"  Store: {store_path}")
