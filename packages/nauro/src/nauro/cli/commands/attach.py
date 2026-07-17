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
from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.cli.utils import refuse_global_config_collision, refuse_repo_config_symlink
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.recovery import RecoveryError, require_cloud_membership, restore_cloud_store
from nauro.store.registry import (
    StoreBindingError,
    bind_project_store_v2,
    get_project_v2,
    get_store_path_v2,
    resolve_v2_from_path,
)
from nauro.store.repo_config import (
    RepoConfigSchemaError,
    load_repo_config,
    repo_config_path,
    save_repo_config,
)
from nauro.store.resolution import DisconnectedProject, resolve_registered_project
from nauro.templates.agents_md_regen import warn_then_regen

_Opt_repo_path = typer.Option(
    None,
    "--repo",
    help="Repo directory to attach. Defaults to cwd.",
)


def _refuse_attach_collision(repo_path: Path, project_id: str) -> None:
    config_path = repo_config_path(repo_path)
    if config_path.is_file():
        try:
            config = load_repo_config(repo_path)
        except (RepoConfigSchemaError, OSError, ValueError):
            config = None
        if config is not None and config.get("id") != project_id:
            typer.echo(
                f"Refusing to overwrite existing .nauro/config.json in "
                f"{repo_path.resolve()}.\n"
                f"  Existing project: {config.get('name')!r} (id: {config.get('id')})\n"
                f"  Requested project id: {project_id}\n"
                "Run 'nauro attach' from a different repo, or remove the stale "
                "association before retrying.",
                err=True,
            )
            raise typer.Exit(code=1)

    resolved = resolve_v2_from_path(repo_path)
    if resolved is None or resolved[0] == project_id:
        return
    existing_id, entry = resolved
    typer.echo(
        f"Repo {repo_path.resolve()} is already part of project "
        f"{entry.get('name', '<unnamed>')!r} (id: {existing_id}).\n"
        "Refusing to attach it to a second project. Remove the existing "
        "association first if it is stale.",
        err=True,
    )
    raise typer.Exit(code=1)


def attach(
    project_id: str = typer.Argument(..., help="Cloud project_id (ULID)."),
    repo_path: Path | None = _Opt_repo_path,
) -> None:
    """Attach the current repo to an existing cloud project."""
    repo_path = repo_path if repo_path is not None else Path.cwd()
    # Refused before the membership call so the failure is local and
    # immediate; the home directory's .nauro/config.json is the global
    # config, not a repo config slot. The symlink refusal precedes the
    # collision check because the collision check reads the repo config,
    # and a planted link must never be read through.
    refuse_global_config_collision(repo_path)
    refuse_repo_config_symlink(repo_path)
    _refuse_attach_collision(repo_path, project_id)
    try:
        name = require_cloud_membership(project_id)
        existing = get_project_v2(project_id)
        server_url = existing.get("server_url", DEFAULT_API_URL) if existing else DEFAULT_API_URL
        connection = resolve_registered_project(project_id)
        if isinstance(connection, DisconnectedProject):
            if connection.reason_code != "connected_record_missing":
                raise RecoveryError(connection.guidance)
            store_path = restore_cloud_store(project_id, connection.store_path)
        elif connection is None:
            store_path = restore_cloud_store(project_id, get_store_path_v2(project_id))
        else:
            store_path = connection.store_path
        bind_project_store_v2(
            project_id=project_id,
            name=name,
            mode=REPO_CONFIG_MODE_CLOUD,
            repo_path=repo_path,
            store_path=store_path,
            server_url=server_url,
        )
    except (RecoveryError, StoreBindingError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    save_repo_config(
        repo_path,
        {
            "mode": REPO_CONFIG_MODE_CLOUD,
            "id": project_id,
            "name": name,
            "server_url": server_url,
        },
    )
    for warning in public_surface_git_warnings(repo_path, ".nauro/config.json"):
        typer.echo(warning, err=True)

    warn_then_regen(
        project_id,
        store_path,
        warn=lambda message: typer.echo(message, err=True),
        fail_soft=True,
    )

    typer.echo(f"Attached '{name}' to {repo_path.resolve()}")
    typer.echo(f"  Project id: {project_id}")
    typer.echo(f"  Store: {store_path}")
