"""nauro note — Add a decision or question to the project store."""

from pathlib import Path

import typer

from nauro.cli.utils import resolve_target_project
from nauro.store.registry import (
    RegistrySchemaError,
    get_project_v2,
    load_registry,
)
from nauro.store.writer import append_decision, append_question
from nauro.templates.agents_md import regenerate_agents_md_for_project


def _registry_repo_paths(project_key: str) -> list[str]:
    """Return repo paths for ``project_key`` from v2 (preferred) or v1 registry.

    Local duplicate of the same-named helper in ``cli.commands.sync``. Kept
    local rather than shared because both call sites are small CLI commands
    and a cross-command helper for two callers is premature.
    """
    try:
        v2_entry = get_project_v2(project_key)
    except RegistrySchemaError:
        v2_entry = None
    if v2_entry is not None:
        return list(v2_entry.get("repo_paths", []))
    registry = load_registry()
    return list(registry["projects"].get(project_key, {}).get("repo_paths", []))


def note(
    text: str = typer.Argument(help="The note content. Ends with '?' to auto-detect as question."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
    question: bool = typer.Option(
        False,
        "--question",
        "-q",
        help="Force treating as a question.",
    ),
    decision: bool = typer.Option(
        False,
        "--decision",
        "-d",
        help="Force treating as a decision (default).",
    ),
    rationale: str | None = typer.Option(
        None,
        "--rationale",
        "-r",
        help="Why this decision was made.",
    ),
    confidence: str = typer.Option(
        "medium",
        "--confidence",
        "-c",
        help="Confidence: high, medium, low.",
    ),
) -> None:
    """Record a decision or question in the project store."""
    project_name, store_path = resolve_target_project(project)

    is_question = question or (text.rstrip().endswith("?") and not decision)

    if is_question:
        append_question(store_path, text)
        typer.echo(f"Question added to {project_name}:")
        typer.echo(f"  {text}")
        typer.echo(f"  File: {store_path / 'open-questions.md'}")
    else:
        filepath = append_decision(
            store_path,
            title=text,
            rationale=rationale,
            confidence=confidence,
        )
        typer.echo(f"Decision recorded in {project_name}:")
        typer.echo(f"  {filepath}")

    # D143: refresh AGENTS.md so MCP-disconnected agents see the update
    # without requiring a separate `nauro sync`. Mirrors the warn-then-regen
    # sequence in `nauro sync` so stale registry paths surface consistently.
    project_key = store_path.name
    for repo_str in _registry_repo_paths(project_key):
        if not Path(repo_str).is_dir():
            typer.echo(
                f"  Warning: repo path does not exist, skipping AGENTS.md: {repo_str}\n"
                f"  Fix: remove from registry or update path in ~/.nauro/registry.json",
                err=True,
            )
    updated_repos = regenerate_agents_md_for_project(project_key, store_path)
    for repo_path in updated_repos:
        typer.echo(f"  Updated AGENTS.md: {repo_path}")
