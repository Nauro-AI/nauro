"""nauro adopt — Bootstrap a project from an existing repo.

Per D125 + D126 + D128, ``nauro adopt`` does in one shot:

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

from pathlib import Path

import typer

from nauro.cli.commands.setup import setup_all_surfaces
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
            f"--name <unique-name>. To attach this repo to that existing "
            f"project, run 'nauro attach {pid}' (cloud-mode) or 'nauro link "
            f"{pid}' (local-mode) instead."
        )
    return None


def adopt(
    name: str | None = typer.Option(
        None, "--name", help="Project name (default: repo directory basename)."
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repo root (default: current working directory)."
    ),
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
) -> None:
    """Adopt an existing repo into Nauro: register, wire MCP, install skills."""
    if print_prompt:
        if name is not None or repo is not None or no_setup_and_skills:
            typer.echo(
                "Error: --print-prompt is mutually exclusive with --name, "
                "--repo, and --no-setup-and-skills.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(load_adopt_body(), nl=False)
        return

    repo_root = _resolve_repo_root(repo)
    if not repo_root.is_dir():
        typer.echo(f"Error: {repo_root} is not a directory.", err=True)
        raise typer.Exit(code=1)

    # ── already-adopted guard ──────────────────────────────────────────────
    config_path = repo_root / ".nauro" / "config.json"
    if config_path.exists():
        typer.echo(
            f"This repo is already adopted (config at {config_path}). To "
            f"start a fresh project from this repo, remove '.nauro/config.json' "
            f"and re-run.",
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
        for line in setup_all_surfaces([repo_root], remove=False):
            typer.echo(line)

    typer.echo(
        "\nNext: restart your agent and invoke /nauro-adopt to seed context "
        "from this repo. Cursor users: if you `git add "
        ".cursor/rules/nauro*.mdc`, collaborators on this repo get both the "
        "/nauro-adopt and /nauro rules."
    )
