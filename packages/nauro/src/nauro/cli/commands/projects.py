"""nauro projects — inspect and recover registry entries.

``nauro projects`` (or ``nauro projects list``) prints every v2 registry
entry: project_id, name, mode, and associated repo paths.

``nauro projects rm <project_id>`` removes a single registry entry. It does
NOT delete the on-disk store directory — decision history under
``~/.nauro/projects/<id>/`` survives so a mistaken removal is recoverable.
This is the documented recovery path when ``nauro init`` refuses to mint a
second entry for a repo that is already claimed.
"""

from __future__ import annotations

from collections import Counter

import typer

from nauro.store.registry import (
    load_registry_v2,
    registered_store_path_hint_v2,
    remove_project_v2,
)

projects_app = typer.Typer(help="Inspect and recover Nauro project registry entries.")


def _print_project_list() -> None:
    registry = load_registry_v2()
    entries = registry["projects"]
    if not entries:
        typer.echo("No projects registered.")
        return
    for pid, entry in entries.items():
        name = entry.get("name", "<unnamed>")
        mode = entry.get("mode", "<unknown>")
        repo_paths = entry.get("repo_paths", [])
        typer.echo(f"{pid}")
        typer.echo(f"  Name: {name}")
        typer.echo(f"  Mode: {mode}")
        if repo_paths:
            for rp in repo_paths:
                typer.echo(f"  Repo: {rp}")
        else:
            typer.echo("  Repo: (none)")

    # Flag duplicate names: v2 allows them, but two same-named projects are
    # separate stores that share no decisions — usually an accidental fork from
    # re-running `nauro init <name>` in a second repo. Surface it so it is
    # discoverable here, not just at creation time.
    name_counts = Counter(e.get("name", "<unnamed>") for e in entries.values())
    dupes = sorted(n for n, c in name_counts.items() if c > 1)
    if dupes:
        typer.echo("")
        typer.echo(
            "Warning: multiple projects share a name: "
            + ", ".join(f"'{n}'" for n in dupes)
            + ". These are separate stores. To put repos under one project, use "
            "`nauro init <name> --add-repo <path>` and drop the extra with "
            "`nauro projects rm <id>`.",
            err=True,
        )


@projects_app.callback(invoke_without_command=True)
def projects_main(ctx: typer.Context) -> None:
    """List registered projects when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        _print_project_list()


@projects_app.command(name="list")
def list_projects() -> None:
    """List every registered project: id, name, mode, and repo paths."""
    _print_project_list()


@projects_app.command(name="rm")
def remove_project(
    project_id: str = typer.Argument(..., help="Project id (ULID) to remove."),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Remove a project's registry entry, leaving its on-disk store intact."""
    registry = load_registry_v2()
    entry = registry["projects"].get(project_id) or {}
    try:
        store_path = registered_store_path_hint_v2(project_id, entry)
    except ValueError as exc:
        # A project_id that escapes the projects root (e.g. ``..`` or an
        # absolute path) trips the containment guard; reject it cleanly
        # rather than surfacing a raw traceback.
        typer.echo(f"Invalid project id {project_id!r}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not yes:
        store_description = (
            str(store_path)
            if store_path is not None
            else "the invalid path recorded in the registry"
        )
        typer.confirm(
            f"Remove registry entry for {project_id}? "
            f"The store at {store_description} will be left intact.",
            abort=True,
        )
    removed = remove_project_v2(project_id)
    if not removed:
        typer.echo(f"No project registered with id {project_id!r}.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Removed registry entry for {project_id}.")
    if store_path is None:
        typer.echo("  Store left untouched: the registered path was invalid.")
    else:
        typer.echo(f"  Store left intact: {store_path}")
