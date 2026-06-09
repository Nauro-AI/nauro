"""nauro adopt — Bootstrap a project from an existing repo.

In one shot it:

  1. Detect repo root (or use --repo).
  2. Guard against re-adopting an already-adopted repo.
  3. Same-name collision pre-check against the local v2 registry — calls
     ``find_projects_by_name_v2`` directly because ``list_projects`` is not
     registered as an MCP tool on local stdio (verified at stdio_server.py),
     and the remote response lacks repo_paths anyway.
  4. Register a v2 project (``register_project_v2`` + ``save_repo_config``)
     and scaffold the store. Bypasses ``nauro init`` because init's
     --add-repo branch silently skips ``save_repo_config`` (init.py:74-101).
  5. Wire MCP and materialize skill files across Claude Code, Cursor, Codex
     via ``setup_all_surfaces``.
  6. Print the closing message instructing the user to restart their agent
     and invoke the ``/nauro-adopt`` skill.

After the user restarts their agent and runs ``/nauro-adopt``, the markdown
skill body (canonical at ``packages/nauro/src/nauro/skills/adopt_body.md``)
walks the agent through reading source files, triaging decisions, and
seeding the Nauro store via the existing MCP write tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from nauro.cli.commands.setup import (
    SHIP_TASK_NEEDS_SUBAGENTS_NOTICE,
    SUBAGENTS_CONNECTOR_NAME_NOTICE,
    _find_nauro_command,
    setup_all_surfaces,
)
from nauro.cli.utils import refuse_global_config_collision
from nauro.constants import REGISTRY_SCHEMA_VERSION_V2, REPO_CONFIG_MODE_LOCAL
from nauro.skills import load_adopt_body
from nauro.store.registry import find_projects_by_name_v2, register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.telemetry import capture
from nauro.telemetry.events import project_created
from nauro.templates.scaffolds import scaffold_project_store


def _resolve_repo_root(repo_arg: Path | None) -> Path:
    """Return the absolute path of the repo root to adopt."""
    return (repo_arg if repo_arg is not None else Path.cwd()).resolve()


def _is_git_repo(repo_root: Path) -> bool:
    """Return True iff ``repo_root`` is inside a git working tree.

    The /nauro-adopt skill's Step 1 runs the same ``git rev-parse`` check and
    aborts when it fails, but its 'run git init, then re-run nauro adopt'
    recovery only works if adopt itself refuses a non-git directory before
    registering it. Without this precondition adopt registered the repo, and
    the recovery then hit the already-adopted guard.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


def _smoke_test_wired_binary(nauro_cmd: str, timeout: float = 1.5) -> str | None:
    """Boot ``<nauro_cmd> serve --stdio`` briefly to verify it doesn't crash on import.

    A healthy stdio server either exits cleanly on stdin EOF (returncode 0) or
    keeps running waiting for an MCP handshake (we kill it after ``timeout``).
    Either outcome is "healthy". The failure mode we care about is the binary
    crashing on import — that surfaces as a non-zero exit before the timeout.

    Returns a multi-line warning string on detected failure, otherwise None.
    """
    try:
        proc = subprocess.run(
            [nauro_cmd, "serve", "--stdio"],
            input="",
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return (
            f"WARNING: could not run `{nauro_cmd} serve --stdio` — binary not found.\n"
            f"  /nauro-adopt and other MCP-driven flows will not work until this is fixed."
        )

    if proc.returncode == 0:
        return None

    first_err = next(
        (line for line in (proc.stderr or "").splitlines() if line.strip()),
        "(no stderr captured)",
    )
    return (
        f"\nWARNING: `{nauro_cmd} serve --stdio` failed to start (exit {proc.returncode}): "
        f"{first_err}\n"
        f"  Run `{nauro_cmd} serve --stdio` manually to see the full traceback.\n"
        f"  /nauro-adopt and other MCP-driven flows will not work until this is fixed."
    )


def _check_collision(name: str, repo_root: Path) -> str | None:
    """Return an error message if a same-name project already exists at a different repo path."""
    matches = find_projects_by_name_v2(name)
    repo_resolved = str(repo_root)
    for pid, entry in matches:
        # Iterate the original list (not a set) so the surfaced path is stable
        # when the colliding project has multiple registered repos.
        existing_paths = [str(Path(p).resolve()) for p in entry.get("repo_paths", [])]
        if repo_resolved in existing_paths:
            continue
        other = existing_paths[0] if existing_paths else "<unknown>"
        return (
            f"A project named '{name}' already exists at '{other}' with id "
            f"'{pid}'. To adopt this repo as a separate project, re-run with "
            f"--name <unique-name>. To associate this repo with that existing "
            f"project instead, run 'nauro init {name!r} --add-repo {repo_root}' "
            f"(local-mode) or 'nauro attach {pid}' (cloud-mode). To drop the "
            f"existing entry, run 'nauro projects rm {pid}'."
        )
    return None


def _install_into_adopted_repo(
    repo_root: Path,
    *,
    with_subagents: bool,
    with_skills: bool,
    force_overwrite: bool,
) -> None:
    """Install bundled subagents/skills onto an already-adopted repo.

    Mirrors the materialize step of a fresh adoption (``setup_all_surfaces``)
    without re-registering the project or rewriting ``.nauro/config.json`` —
    the repo is already adopted, so registration is intact and untouched.
    Lets ``nauro adopt --with-subagents`` (or ``--with-skills``) add the
    bundled artifacts to an existing adoption instead of aborting.
    """
    typer.echo("Repo already adopted. Installing requested artifacts across surfaces:\n")
    for line in setup_all_surfaces(
        [repo_root],
        remove=False,
        with_subagents=with_subagents,
        force_overwrite=force_overwrite,
        with_skills=with_skills,
    ):
        typer.echo(line)
    if with_skills and not with_subagents:
        typer.echo(f"\n{SHIP_TASK_NEEDS_SUBAGENTS_NOTICE}")
    if with_subagents:
        typer.echo(f"\n{SUBAGENTS_CONNECTOR_NAME_NOTICE}")
    typer.echo("\nNext: restart your agent so it picks up the newly installed files.")


_Opt_repo = typer.Option(None, "--repo", help="Repo root (default: current working directory).")


def adopt(
    name: str | None = typer.Option(
        None, "--name", help="Project name (default: repo directory basename)."
    ),
    repo: Path | None = _Opt_repo,
    print_prompt: bool = typer.Option(
        False,
        "--print-prompt",
        help=(
            "Print the canonical /nauro-adopt skill body to stdout and exit. "
            "Use to copy/paste into chat surfaces. Mutually exclusive with "
            "other flags."
        ),
    ),
    no_setup_and_skills: bool = typer.Option(
        False,
        "--no-setup-and-skills",
        help="Skip MCP wiring + skill materialization (for users with existing wiring).",
    ),
    with_subagents: bool = typer.Option(
        False,
        "--with-subagents",
        help=(
            "Install Nauro's bundled workflow subagents (@nauro-planner, "
            "@nauro-executor, @nauro-reviewer, @nauro-tech-lead) into "
            "~/.claude/agents/. Off by default to avoid overwriting "
            "customized files."
        ),
    ),
    force_overwrite: bool = typer.Option(
        False,
        "--force-overwrite",
        help=(
            "Overwrite ~/.claude/agents/nauro-*.md in place without saving a "
            ".bak, when --with-subagents is passed. By default, install "
            "refreshes a differing bundled file and stashes its prior content "
            "to <name>.md.bak."
        ),
    ),
    with_skills: bool = typer.Option(
        False,
        "--with-skills",
        help=(
            "Install Nauro's bundled opt-in skills "
            "(/nauro-ship-task, /nauro-handoff, /nauro-context) alongside the "
            "always-installed /nauro-adopt skill. Independent of --with-subagents."
        ),
    ),
) -> None:
    """Adopt an existing repo into Nauro: register, wire MCP, install skills."""
    if print_prompt:
        if (
            name is not None
            or repo is not None
            or no_setup_and_skills
            or with_subagents
            or force_overwrite
            or with_skills
        ):
            typer.echo(
                "Error: --print-prompt is mutually exclusive with --name, "
                "--repo, --no-setup-and-skills, --with-subagents, "
                "--force-overwrite, and --with-skills.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(load_adopt_body(), nl=False)
        return

    repo_root = _resolve_repo_root(repo)
    if not repo_root.is_dir():
        typer.echo(f"Error: {repo_root} is not a directory.", err=True)
        raise typer.Exit(code=1)

    # Refused before the git and already-adopted checks: from the home
    # directory the global config would otherwise read as an existing
    # adoption, and the recovery hint there ("remove .nauro/config.json")
    # would point at the user's auth and telemetry settings.
    refuse_global_config_collision(repo_root)

    # ── git precondition ───────────────────────────────────────────────────
    # Refuse before any registration or config write so the /nauro-adopt
    # skill's 'git init, then re-run' recovery actually works.
    if not _is_git_repo(repo_root):
        typer.echo(
            "Error: nauro adopt must be run inside a git repository. "
            "Run `git init` first, or pass --repo <path> to point at one.",
            err=True,
        )
        raise typer.Exit(code=1)

    # ── already-adopted guard ──────────────────────────────────────────────
    config_path = repo_root / ".nauro" / "config.json"
    if config_path.exists():
        # Already adopted. If the caller asked to install bundled subagents or
        # skills, route to the materialize step for the existing adoption rather
        # than dead-ending — those flags are otherwise unreachable on `adopt`
        # once a repo is adopted, and `adopt` is the command users reach for
        # first. Registration and config.json are left untouched, so the
        # invariant that every adoption writes config.json is unaffected.
        if with_subagents or with_skills or force_overwrite:
            _install_into_adopted_repo(
                repo_root,
                with_subagents=with_subagents,
                with_skills=with_skills,
                force_overwrite=force_overwrite,
            )
            return
        typer.echo(
            f"This repo is already adopted (config at {config_path}). To add "
            f"Nauro's bundled subagents or skills, re-run with --with-subagents "
            f"and/or --with-skills. To start a fresh project from this repo, "
            f"remove '.nauro/config.json' and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)

    project_name = name if name is not None else repo_root.name

    # ── same-name collision pre-check ──────────────────────────────────────
    collision = _check_collision(project_name, repo_root)
    if collision is not None:
        typer.echo(collision, err=True)
        raise typer.Exit(code=1)

    # ── mint project + write per-repo config + scaffold store ──────────────
    try:
        pid, store_path = register_project_v2(
            project_name,
            [repo_root],
            mode=REPO_CONFIG_MODE_LOCAL,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    capture("project.created", project_created(REGISTRY_SCHEMA_VERSION_V2))

    save_repo_config(
        repo_root,
        {
            "mode": REPO_CONFIG_MODE_LOCAL,
            "id": pid,
            "name": project_name,
        },
    )
    scaffold_project_store(project_name, store_path)

    typer.echo(f"Adopted project '{project_name}' (id: {pid})")
    typer.echo(f"  Store: {store_path}")
    typer.echo(f"  Repo:  {repo_root}")

    # ── wire MCP + materialize skills ──────────────────────────────────────
    if not no_setup_and_skills:
        typer.echo("\nWiring MCP and installing skills across surfaces:")
        for line in setup_all_surfaces(
            [repo_root],
            remove=False,
            current_project_key=pid,
            store_path=store_path,
            with_subagents=with_subagents,
            force_overwrite=force_overwrite,
            with_skills=with_skills,
        ):
            typer.echo(line)

        if with_skills and not with_subagents:
            typer.echo(f"\n{SHIP_TASK_NEEDS_SUBAGENTS_NOTICE}")

        if with_subagents:
            typer.echo(f"\n{SUBAGENTS_CONNECTOR_NAME_NOTICE}")

        warning = _smoke_test_wired_binary(_find_nauro_command())
        if warning:
            typer.echo(warning, err=True)

    typer.echo(
        "\nNext: restart your agent and invoke /nauro-adopt to seed context "
        "from this repo. Cursor users: if you `git add "
        ".cursor/rules/nauro-adopt.mdc`, collaborators on this repo get the "
        "/nauro-adopt rule."
    )
