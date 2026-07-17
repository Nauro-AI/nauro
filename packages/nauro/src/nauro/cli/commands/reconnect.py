"""nauro reconnect: connect a repository's known project record."""

from __future__ import annotations

from pathlib import Path

import typer

from nauro.store.recovery import (
    RecoveryError,
    bind_local_store,
    require_cloud_membership,
    restore_cloud_store,
)
from nauro.store.registry import StoreBindingError, bind_project_store_v2
from nauro.store.repo_config import find_repo_config, load_repo_config, save_repo_config
from nauro.store.resolution import DisconnectedProject, RepoResolution, resolve_from_cwd
from nauro.templates.agents_md_regen import warn_then_regen


def _render_actions(actions: tuple[str, ...]) -> None:
    typer.echo("\nAvailable actions:")
    labels = {
        "locate": "Locate an existing local record",
        "restore": "Restore the latest eligible cloud record",
        "continue": "Continue without Nauro for now",
    }
    for action in actions:
        typer.echo(f"  {action}: {labels[action]}")


def _finish_connection(repo_root: Path, store_path: Path, project_id: str) -> None:
    warn_then_regen(
        project_id,
        store_path,
        warn=lambda message: typer.echo(message, err=True),
        fail_soft=True,
    )


def reconnect() -> None:
    """Connect or recover the project already named by this repository."""
    config_path = find_repo_config(start=Path.cwd())
    if config_path is None:
        typer.echo(
            "No Nauro project config found in this directory. Run this command from an "
            "adopted repository.",
            err=True,
        )
        raise typer.Exit(code=1)
    repo_root = config_path.parent.parent
    connection = resolve_from_cwd(repo_root)
    if isinstance(connection, RepoResolution):
        typer.echo(f"Already connected to '{connection.display_name}'.")
        typer.echo(f"  Store: {connection.store_path}")
        return
    if not isinstance(connection, DisconnectedProject):
        typer.echo("The repository project config could not be validated.", err=True)
        raise typer.Exit(code=1)

    typer.echo(connection.guidance)
    _render_actions(connection.recovery_actions)
    action = typer.prompt("Action", default="continue").strip().lower()
    if action not in connection.recovery_actions:
        typer.echo(
            f"Unknown action {action!r}. Choose one of: " + ", ".join(connection.recovery_actions),
            err=True,
        )
        raise typer.Exit(code=1)
    if action == "continue":
        typer.echo("No changes made. Nauro-dependent workflows remain unavailable.")
        return

    try:
        if action == "locate":
            store_path = Path(typer.prompt("Absolute store path"))
            resolved = bind_local_store(repo_root, store_path)
            _finish_connection(repo_root, resolved.store_path, resolved.project_id)
            typer.echo(f"Connected '{resolved.display_name}' to {resolved.store_path}")
            return

        remote_name = require_cloud_membership(connection.project_id)
        store_path = restore_cloud_store(connection.project_id, connection.store_path)
        config = load_repo_config(repo_root)
        # Membership was just verified, so the cloud name is authoritative:
        # a server-side rename reconciles the registry and repo config here
        # rather than leaving every later resolution in a binding conflict.
        bound = bind_project_store_v2(
            project_id=connection.project_id,
            name=remote_name,
            mode=config["mode"],
            repo_path=repo_root,
            store_path=store_path,
            server_url=config.get("server_url"),
            update_name=True,
        )
        if remote_name != config.get("name"):
            typer.echo(f"Cloud project is now named {remote_name!r}; updating the local records.")
            save_repo_config(repo_root, {**config, "name": remote_name})
        _finish_connection(repo_root, bound, connection.project_id)
        typer.echo(f"Restored and connected '{remote_name}'.")
        typer.echo(f"  Store: {bound}")
    except (RecoveryError, StoreBindingError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
