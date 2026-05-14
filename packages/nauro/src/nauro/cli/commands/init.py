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
from nauro.store.repo_config import (
    RepoConfigSchemaError,
    load_repo_config,
    repo_config_path,
    save_repo_config,
)
from nauro.sync.cloud_projects import CloudProjectError, create_project
from nauro.telemetry import capture
from nauro.telemetry.events import project_created
from nauro.templates.scaffolds import scaffold_project_store

logger = logging.getLogger("nauro.cli.init")


def _check_config_overwrite(
    rp: Path,
    expected_id: str | None,
    expected_name: str,
    force: bool,
) -> None:
    """Refuse to overwrite an existing ``.nauro/config.json`` whose project
    differs from the one being initialized. Per D136, this closes the
    silent-overwrite footgun where ``nauro init <new-name>`` (or
    ``nauro init --demo``) would replace a real project's cwd config
    without warning, breaking every subsequent cwd-walk-up resolution.

    No-op when no existing config is present, when the existing config
    advertises the same project *id* as ``expected_id`` (idempotent
    re-write — applies to ``--add-repo`` where the pid is known up front),
    or when ``force`` is set. Aborts via :class:`typer.Exit` with a
    diagnostic message naming the existing project otherwise. For a fresh
    init where ``expected_id`` is ``None``, no id match can short-circuit,
    so any existing config triggers the refusal — name match alone is not
    a safe idempotency signal because v2 allows duplicate names with
    distinct ids.
    """
    config_file = repo_config_path(rp)
    if not config_file.is_file():
        return
    try:
        existing = load_repo_config(rp)
    except RepoConfigSchemaError:
        # Existing file is structurally invalid — let save_repo_config
        # replace it; there is no trustworthy state to preserve.
        return
    except (OSError, ValueError):
        return
    existing_id = existing.get("id")
    existing_name = existing.get("name")
    # Idempotent: --add-repo against the same project id is a re-statement,
    # not a conflict. We only short-circuit on id match — name match is
    # insufficient because v2 allows duplicate names with distinct ids.
    if expected_id is not None and existing_id == expected_id:
        return
    if force:
        return
    typer.echo(
        f"Refusing to overwrite existing .nauro/config.json in {rp.resolve()}\n"
        f"  Existing: {existing_name!r} (id: {existing_id})\n"
        f"  New:      {expected_name!r}\n"
        "\n"
        "Options:\n"
        "  - Re-run with --force to replace the existing config.\n"
        "  - cd into a different directory and re-run nauro init.\n"
        f"  - If you meant to associate this repo with {existing_name!r},\n"
        f"    run: nauro init {existing_name!r} --add-repo {rp.resolve()}",
        err=True,
    )
    raise typer.Exit(code=1)


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
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite an existing .nauro/config.json in the target repo. "
            "Without this flag init refuses to replace a config pointing at "
            "a different project."
        ),
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
            # Pre-check every target repo before any state changes (D136).
            for rp in repo_paths:
                _check_config_overwrite(rp, pid, name, force)
            added = []
            for rp in repo_paths:
                add_repo_v2(pid, rp)
                # Per-repo config is the source of truth for "is this repo
                # adopted?" (D111). The cloud-mode branch is rejected above,
                # so all surviving entries here are local-mode.
                save_repo_config(
                    rp,
                    {
                        "mode": REPO_CONFIG_MODE_LOCAL,
                        "id": pid,
                        "name": name,
                    },
                )
                added.append(rp.resolve())
            typer.echo(f"Updated project '{name}'")
            typer.echo(f"  Store: {store_path}")
            for rp in added:
                typer.echo(f"  Added repo: {rp}")
            return

    # ── New project: cloud or local ────────────────────────────────────────
    # Pre-check every target repo before allocating a new id (D136). For a
    # fresh init we have no pid to compare against; any existing config is
    # treated as a potential conflict and refused without --force. v2 allows
    # duplicate names with distinct ids, so name-match alone cannot be a
    # safe idempotency signal — silently coalescing 'nauro init projA' from
    # a cwd already linked to a different projA would lose the user's
    # existing project association.
    for rp in repo_paths:
        _check_config_overwrite(rp, None, name, force)

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
