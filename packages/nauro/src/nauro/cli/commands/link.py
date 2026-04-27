"""nauro link — Promote a local-only project to cloud.

``nauro link --cloud`` reads the local-mode ``.nauro/config.json`` in the
current repo, calls the remote MCP server's ``POST /projects`` to mint a
cloud project_id, then re-keys the local store directory and v2 registry
entry under the new id while preserving repo_paths and store contents.

The flow is intentionally one-way: there is no inverse "unlink to local"
command. The CLI only opens up new escape hatches when there is a real
incident that demands them.
"""

from __future__ import annotations

import typer

from nauro.cli.commands.auth import DEFAULT_API_URL
from nauro.constants import (
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
)
from nauro.store.registry import (
    get_project_v2,
    rename_project_id_v2,
)
from nauro.store.repo_config import (
    RepoConfigSchemaError,
    find_repo_config,
    load_repo_config,
    save_repo_config,
)
from nauro.sync.cloud_projects import CloudProjectError, create_project


def link(
    cloud: bool = typer.Option(
        False,
        "--cloud",
        help="Promote the current repo's local-only project to a cloud project.",
    ),
) -> None:
    """Promote a local-only project to cloud-mode."""
    if not cloud:
        typer.echo(
            "nauro link requires a target. Did you mean: nauro link --cloud?",
            err=True,
        )
        raise typer.Exit(code=1)

    config_path = find_repo_config()
    if config_path is None:
        typer.echo(
            "Not a nauro repo: no .nauro/config.json found above the current "
            "directory. Run 'nauro init <name>' first.",
            err=True,
        )
        raise typer.Exit(code=1)

    repo_root = config_path.parent.parent
    try:
        cfg = load_repo_config(repo_root)
    except RepoConfigSchemaError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    if cfg.get("mode") != REPO_CONFIG_MODE_LOCAL:
        typer.echo(
            f"Project '{cfg.get('name')}' is already cloud-mode "
            f"(id {cfg.get('id')}); nothing to link.",
            err=True,
        )
        raise typer.Exit(code=1)

    local_id = cfg["id"]
    name = cfg["name"]

    if get_project_v2(local_id) is None:
        typer.echo(
            f"Local project id {local_id!r} is not in the v2 registry. "
            "Did you migrate ~/.nauro/registry.json?",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        view = create_project(name)
    except CloudProjectError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    cloud_id = view["project_id"]

    try:
        new_store = rename_project_id_v2(
            local_id,
            cloud_id,
            mode=REPO_CONFIG_MODE_CLOUD,
            server_url=DEFAULT_API_URL,
        )
    except (KeyError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    save_repo_config(
        repo_root,
        {
            "mode": REPO_CONFIG_MODE_CLOUD,
            "id": cloud_id,
            "name": name,
            "server_url": DEFAULT_API_URL,
        },
    )

    typer.echo(f"Linked '{name}' to cloud project")
    typer.echo(f"  Old id: {local_id}")
    typer.echo(f"  New id: {cloud_id}")
    typer.echo(f"  Store:  {new_store}")
