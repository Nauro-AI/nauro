"""Option declarations and install notices shared by ``nauro adopt`` and ``nauro setup all``."""

from __future__ import annotations

import typer

from nauro.cli.integrations.orchestrator import (
    SHIP_TASK_NEEDS_SUBAGENTS_NOTICE,
    SUBAGENTS_CONNECTOR_NAME_NOTICE,
)

WITH_SUBAGENTS_OPTION = typer.Option(
    False,
    "--with-subagents",
    help=(
        "Install Nauro's bundled workflow subagents (nauro-planner, "
        "nauro-executor, nauro-reviewer, nauro-tech-lead, nauro-investigator) into "
        "~/.claude/agents/ for Claude Code, .cursor/agents/ in each registered "
        "repo for Cursor when Cursor configuration is found, and ~/.codex/agents/ "
        "for Codex. Off by default."
    ),
)

FORCE_OVERWRITE_OPTION = typer.Option(
    False,
    "--force-overwrite",
    help=(
        "Overwrite differing bundled Nauro skills and agent definitions "
        "without saving .bak files. By default, refresh preserves the "
        "previous content in a sibling backup."
    ),
)

WITH_SKILLS_OPTION = typer.Option(
    False,
    "--with-skills",
    help=(
        "Install Nauro's bundled opt-in skills "
        "(nauro-ship-task, nauro-context, nauro-loop, nauro-interview) alongside the "
        "always-installed nauro-adopt skill. Independent of --with-subagents."
    ),
)


def echo_install_notices(with_skills: bool, with_subagents: bool) -> None:
    """Echo the follow-up notices an install with these opt-in flags owes the user."""
    if with_skills and not with_subagents:
        typer.echo(f"\n{SHIP_TASK_NEEDS_SUBAGENTS_NOTICE}")
    if with_subagents:
        typer.echo(f"\n{SUBAGENTS_CONNECTOR_NAME_NOTICE}")
