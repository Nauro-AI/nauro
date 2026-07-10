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

from pathlib import Path

import typer

from nauro.cli.commands.auth import DEFAULT_API_URL
from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.cli.utils import refuse_global_config_collision
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
    resolve_v2_from_path,
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


def _echo_repo_config_warnings(repo_path: Path) -> None:
    for warning in public_surface_git_warnings(repo_path, ".nauro/config.json"):
        typer.echo(warning, err=True)


def _check_config_overwrite(
    rp: Path,
    expected_id: str | None,
    expected_name: str,
    force: bool,
) -> None:
    """Refuse to overwrite an existing ``.nauro/config.json`` whose project
    differs from the one being initialized. Closes the silent-overwrite
    footgun where ``nauro init <new-name>`` (or ``nauro init --demo``)
    would replace a real project's cwd config without warning, breaking
    every subsequent cwd-walk-up resolution.

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


def _refuse_if_repo_already_claimed(rp: Path) -> None:
    """Refuse to mint a new project for a repo an existing project already claims.

    A repo path resolves to at most one project. If ``rp`` already walks up to
    a registered project, minting a second id here would shadow that
    association and leave a duplicate registry entry the user never intended.
    Aborts via :class:`typer.Exit` naming the safe recovery paths.
    """
    resolved = resolve_v2_from_path(rp)
    if resolved is None:
        return
    existing_id, entry = resolved
    existing_name = entry.get("name", "<unnamed>")
    typer.echo(
        f"Repo {rp.resolve()} is already part of project "
        f"{existing_name!r} (id: {existing_id}).\n"
        "Refusing to register a second project for the same repo.\n"
        "\n"
        "Options:\n"
        f"  - Add this repo to the existing project: "
        f"nauro init {existing_name!r} --add-repo {rp.resolve()}\n"
        f"  - Remove the existing registry entry: nauro projects rm {existing_id}\n"
        "  - Promote a local project to cloud: nauro link",
        err=True,
    )
    raise typer.Exit(code=1)


def _warn_if_name_taken(name: str, new_pid: str, pre_existing: list) -> None:
    """Warn when a fresh init created a second project sharing an existing name.

    v2 allows duplicate names, but two same-named projects are separate stores
    that share no decisions — rarely what a user re-running ``nauro init`` from
    another repo intends, and it silently defeats the cross-repo promise. The
    repo-path collision is already refused by ``_refuse_if_repo_already_claimed``;
    this surfaces the name collision and points at the association path instead
    of failing silently. Advisory only — the project is still created.
    """
    others = [pid for pid, _entry in pre_existing if pid != new_pid]
    if not others:
        return
    typer.echo(
        f"  Note: another local project is also named '{name}' (id {others[0]}). "
        "This is a SEPARATE store; the two repos will not share decisions.",
        err=True,
    )
    typer.echo(
        f"  To put this repo under the existing project instead, run "
        f"'nauro projects rm {new_pid}' then 'nauro init {name} --add-repo .'.",
        err=True,
    )


def _init_demo(name: str, repo_paths: list[Path], force: bool) -> None:
    """Initialize (or reuse) the bundled demo project.

    Demo init has its own idempotency contract that the generic new-project
    flow does not: a single shared demo entry is reused rather than
    duplicated, and the cwd-config overwrite message is demo-specific.
    """
    from nauro.demo import DEMO_DECISIONS, create_demo_project

    # Demo-specific overwrite guard: when the cwd already carries a config and
    # --force is absent, surface a reset-oriented message instead of the
    # generic --add-repo recovery line. Only the cwd (first/only path) gets the
    # demo-config; --demo does not take --add-repo, so repo_paths is [cwd].
    for rp in repo_paths:
        config_file = repo_config_path(rp)
        if config_file.is_file() and not force:
            typer.echo(
                f"Demo already initialized here ({rp.resolve()}).\n"
                "Re-run with --force to reset it.",
                err=True,
            )
            raise typer.Exit(code=1)

    # Reuse an existing demo entry rather than minting a duplicate.
    existing = find_projects_by_name_v2(name)
    if existing:
        pid, _entry = existing[0]
        store_path = get_store_path_v2(pid)
        typer.echo(f"Demo project already exists ({pid}); reusing it.")
    else:
        # No pre-existing demo entry: refuse if any target repo is already
        # claimed by a *different* project before minting a new id.
        for rp in repo_paths:
            _refuse_if_repo_already_claimed(rp)
        pid, store_path = register_project_v2(
            name,
            repo_paths,
            mode=REPO_CONFIG_MODE_LOCAL,
        )
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
        _echo_repo_config_warnings(rp)

    create_demo_project(store_path)
    cwd_is_git = (Path.cwd() / ".git").is_dir()

    typer.echo(f"Initialized demo project '{name}'")
    typer.echo(f"  Project id: {pid}")
    typer.echo(f"  Store: {store_path}")
    for rp in repo_paths:
        typer.echo(f"  Repo:  {rp.resolve()}")
    typer.echo(
        f"  Includes: {len(DEMO_DECISIONS)} decisions, project state, "
        "open questions, and a snapshot"
    )
    typer.echo(f"  Wrote .nauro/config.json into {Path.cwd()}")
    if cwd_is_git:
        typer.echo(
            "  Warning: this directory is a git repo; the demo config will steer "
            "its resolution to the demo doctrine.",
            err=True,
        )
    typer.echo("  Next: run 'nauro check-decision \"<approach>\"' to try a conflict check")


_Opt_add_repo_paths = typer.Option(
    None,
    "--add-repo",
    help="Repo directory to associate (can be repeated). Defaults to cwd.",
)


def init(
    name: str | None = typer.Argument(
        default=None,
        help="Project name. Defaults to the directory name (or 'demo-project' with --demo).",
    ),
    add_repo_paths: list[Path] | None = _Opt_add_repo_paths,
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Create a sample project with pre-written decisions.",
    ),
    cloud: bool = typer.Option(
        False,
        "--cloud",
        help=(
            "Create a cloud-scoped project on the remote MCP server. "
            "Requires prior 'nauro auth login'."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite the .nauro/config.json in the current directory only. "
            "Without this flag init refuses to replace a config pointing at a "
            "different project. This does not replace the project itself."
        ),
    ),
) -> None:
    """Initialize a new Nauro project store and register it.

    If a project with the given name already exists locally and --add-repo
    is provided, the repos are appended to the existing local-mode entry.
    Cloud-mode entries cannot be extended this way — use 'nauro attach'.
    """
    # --demo seeds pre-written decisions directly to disk; the --cloud path
    # goes through propose_decision, which has no batch-seed bypass. Reject
    # the combination at command entry rather than silently dropping --demo
    # inside the --cloud branch.
    if demo and cloud:
        raise typer.BadParameter(
            "Cannot combine --demo with --cloud — the demo fixture seeds "
            "locally only. Use `nauro init <name> --demo` for a local demo, "
            "or `nauro init <name> --cloud` for an empty cloud project.",
            param_hint="--demo / --cloud",
        )

    # Resolve an omitted name. --demo keeps its fixed sample name; otherwise
    # derive from the current directory (like `git init`/`npm init`) instead of
    # silently creating a real, empty project literally named 'demo-project'.
    if name is None:
        name = "demo-project" if demo else Path.cwd().name
        if not name:
            typer.echo(
                "Error: could not derive a project name from the current directory. "
                "Pass one explicitly: nauro init <name>",
                err=True,
            )
            raise typer.Exit(code=1)

    repo_paths = add_repo_paths if add_repo_paths else [Path.cwd()]

    # The home directory is never a valid repo root: its .nauro/config.json
    # is the global config (auth tokens, telemetry consent). Refused for every
    # target path before any registry or store mutation, and deliberately
    # before the --force-aware overwrite checks below, which must not be able
    # to bypass it. Previously `nauro init --demo` from $HOME hit the generic
    # config-exists branch, whose "Re-run with --force" hint replaced the
    # global config.
    for rp in repo_paths:
        refuse_global_config_collision(rp)

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
            # Pre-check every target repo before any state changes.
            for rp in repo_paths:
                _check_config_overwrite(rp, pid, name, force)
            added = []
            for rp in repo_paths:
                add_repo_v2(pid, rp)
                # Per-repo config is the source of truth for "is this repo
                # adopted?". The cloud-mode branch is rejected above, so all
                # surviving entries here are local-mode.
                save_repo_config(
                    rp,
                    {
                        "mode": REPO_CONFIG_MODE_LOCAL,
                        "id": pid,
                        "name": name,
                    },
                )
                _echo_repo_config_warnings(rp)
                added.append(rp.resolve())
            typer.echo(f"Updated project '{name}'")
            typer.echo(f"  Store: {store_path}")
            for rp in added:
                typer.echo(f"  Added repo: {rp}")
            return

    # ── --demo ───────────────────────────────────────────────────────────────
    # Handled before the generic new-project flow because demo has its own
    # idempotency rules: a single shared 'demo-project' entry is reused rather
    # than duplicated, and the cwd-config overwrite message is demo-specific.
    if demo:
        _init_demo(name, repo_paths, force)
        return

    # ── New project: cloud or local ────────────────────────────────────────
    # Pre-check every target repo before allocating a new id. For a fresh
    # init we have no pid to compare against; any existing config is treated
    # as a potential conflict and refused without --force. v2 allows
    # duplicate names with distinct ids, so name-match alone cannot be a
    # safe idempotency signal — silently coalescing 'nauro init projA' from
    # a cwd already linked to a different projA would lose the user's
    # existing project association.
    for rp in repo_paths:
        _check_config_overwrite(rp, None, name, force)

    # Refuse to mint a second entry for a repo a project already claims. A
    # repo's identity is single-valued; minting a new id would shadow the
    # existing association and leave a duplicate the user never intended.
    for rp in repo_paths:
        _refuse_if_repo_already_claimed(rp)

    if cloud:
        try:
            view = create_project(name)
        except CloudProjectError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
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
            raise typer.Exit(code=1) from exc
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
            _echo_repo_config_warnings(rp)
        scaffold_project_store(name, store_path)
        typer.echo(f"Initialized cloud project '{name}'")
        typer.echo(f"  Project id: {pid}")
        typer.echo(f"  Store: {store_path}")
        for rp in repo_paths:
            typer.echo(f"  Repo:  {rp.resolve()}")
        typer.echo("  Next: run 'nauro setup claude-code' to connect your agent")
        typer.echo("  Then: run 'nauro sync' to capture the first snapshot")
        return

    # ── Local-only ─────────────────────────────────────────────────────────
    # Captured before minting so a same-name match is necessarily a different
    # repo (a same-repo claim was already refused above).
    pre_existing_same_name = find_projects_by_name_v2(name)
    try:
        pid, store_path = register_project_v2(
            name,
            repo_paths,
            mode=REPO_CONFIG_MODE_LOCAL,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
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
        _echo_repo_config_warnings(rp)

    scaffold_project_store(name, store_path)
    typer.echo(f"Initialized project '{name}'")
    typer.echo(f"  Project id: {pid}")
    typer.echo(f"  Store: {store_path}")
    for rp in repo_paths:
        typer.echo(f"  Repo:  {rp.resolve()}")
    _warn_if_name_taken(name, pid, pre_existing_same_name)
    typer.echo("  Next: run 'nauro setup claude-code' to connect your agent")
    typer.echo("  Then: run 'nauro sync' to capture the first snapshot")
