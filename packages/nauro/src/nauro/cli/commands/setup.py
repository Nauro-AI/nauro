"""nauro setup — Configure tool integrations.

Subcommands:
  nauro setup claude-code  — register MCP server in <repo>/.mcp.json (project scope)
                             for each of the project's repos
  nauro setup cursor       — register MCP server in <repo>/.cursor/mcp.json
                             for each of the project's repos
  nauro setup codex        — register MCP server in ~/.codex/config.toml
"""

from __future__ import annotations

from pathlib import Path

import typer

from nauro.cli.integrations.orchestrator import (
    SHIP_TASK_NEEDS_SUBAGENTS_NOTICE,
    SUBAGENTS_CONNECTOR_NAME_NOTICE,
    claude_code_surfaces,
    codex_surfaces,
    cursor_surfaces,
    setup_all_surfaces,
)
from nauro.cli.integrations.render import render
from nauro.cli.utils import _resolve_project_entry, resolve_target_project

setup_app = typer.Typer(help="Configure tool integrations.")

# Discoverability hint appended to every setup-add success path.
# `nauro check-decision` (the L1 surface) works from the current shell
# against the local store, so users don't have to wait for an agent
# restart to see Nauro do something useful.
CHECK_HINT_LINE = 'Try it now from this shell: nauro check-decision "<approach>"'


@setup_app.command(name="claude-code")
def claude_code(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's advisory UserPromptSubmit hook into each repo's "
            "machine-local .claude/settings.local.json. The hook surfaces "
            "related decisions as context each turn and never blocks."
        ),
    ),
) -> None:
    """Configure Claude Code to use Nauro during sessions."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)
    project_repos = [Path(rp) for rp in entry["repo_paths"]]

    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}':\n")
    for outcome in claude_code_surfaces(
        project_repos,
        remove=remove,
        with_hooks=with_hooks,
        store_name=_store_path.name,
        store_path=_store_path,
        warn=lambda msg: typer.echo(msg, err=True),
    ):
        for line in render(outcome):
            typer.echo(line)

    if not remove:
        typer.echo(
            "\nNext: start a Claude Code session in one of the repos."
            " The MCP server will start automatically."
        )
        if with_hooks:
            typer.echo(f"\n{HOOKS_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Cursor ─────────────────────────────────────────────────────────────────


# Cursor reads MCP servers from `<repo>/.cursor/mcp.json` (per-project).
# User-global "Rules for AI" live in the IDE Settings UI, not a file path —
# so MCP wiring is per-project here.
# Docs: https://cursor.com/docs


@setup_app.command(name="cursor")
def cursor(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
) -> None:
    """Configure Cursor to use Nauro for this project's repos."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)
    project_repos = [Path(rp) for rp in entry["repo_paths"]]

    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro (Cursor) for project '{project_name}':\n")
    for outcome in cursor_surfaces(project_repos, remove=remove):
        for line in render(outcome):
            typer.echo(line)

    if not remove:
        typer.echo("\nNext: open this repo in Cursor and start a chat - Nauro MCP will connect.")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Codex CLI ──────────────────────────────────────────────────────────────


# Codex reads MCP servers from `~/.codex/config.toml` under `[mcp_servers.<name>]`.
# This is the user-global Codex CLI config and is shared with the IDE extension.
# Docs: https://developers.openai.com/codex/mcp


@setup_app.command(name="codex")
def codex(
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's SessionStart and SubagentStart hooks into each repo's "
            "project-scope .codex/hooks.json. Codex requires review through /hooks. "
            "Removal cleans up existing hooks without this flag."
        ),
    ),
) -> None:
    """Configure Codex CLI to use Nauro (writes '~/.codex/config.toml')."""
    for outcome in codex_surfaces(remove=remove, with_hooks=with_hooks):
        for line in render(outcome):
            typer.echo(line)

    if not remove:
        typer.echo("\nNext: run a Codex session - it reads ~/.codex/config.toml on start.")
        if with_hooks:
            typer.echo(f"\n{CODEX_HOOKS_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── nauro setup all ────────────────────────────────────────────────────────


HOOKS_NOTICE = (
    "The advisory hook surfaces related decisions as context on each turn "
    "(BM25 retrieval) and never blocks. Start a new Claude Code session in a "
    "wired repo for it to take effect."
)

CODEX_HOOKS_NOTICE = (
    "Codex skips new or changed hooks until you review and trust them. Start "
    "Codex in a wired repo, then open `/hooks` to review the project hooks."
)

# Multi-surface restart handoff. MCP config is read at session start, so an
# already-open session won't see the new wiring until it restarts. The
# single-tool `setup claude-code` prints its own equivalent line.
ALL_RESTART_NOTICE = (
    "Next: start a fresh agent session (Claude Code/Cursor) - MCP config is read at session start."
)


@setup_app.command(name="all")
def all_(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
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
            "(/nauro-ship-task, /nauro-context, /nauro-loop) alongside the "
            "always-installed /nauro-adopt skill. Independent of --with-subagents."
        ),
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's advisory Claude Code UserPromptSubmit hook and Codex "
            "SessionStart/SubagentStart hooks into each repo's project-scope "
            "configuration. Hooks surface decision context and never block."
        ),
    ),
) -> None:
    """Configure Claude Code, Cursor, and Codex CLI in one call."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)

    project_repos = [Path(rp) for rp in entry["repo_paths"]]
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}' across all surfaces:\n")
    for outcome in setup_all_surfaces(
        project_repos,
        remove=remove,
        current_project_key=_store_path.name,
        store_path=_store_path,
        with_subagents=with_subagents,
        force_overwrite=force_overwrite,
        with_skills=with_skills,
        with_hooks=with_hooks,
    ):
        for line in render(outcome):
            typer.echo(line)

    if not remove and with_skills and not with_subagents:
        typer.echo(f"\n{SHIP_TASK_NEEDS_SUBAGENTS_NOTICE}")

    if not remove and with_subagents:
        typer.echo(f"\n{SUBAGENTS_CONNECTOR_NAME_NOTICE}")

    if not remove and with_hooks:
        typer.echo(f"\n{HOOKS_NOTICE}")
        typer.echo(f"\n{CODEX_HOOKS_NOTICE}")

    if not remove:
        typer.echo(f"\n{ALL_RESTART_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")
