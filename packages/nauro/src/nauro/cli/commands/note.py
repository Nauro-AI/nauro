"""nauro note — Add a decision or question to the project store."""

import typer

from nauro.cli.utils import resolve_target_project
from nauro.store.writer import append_decision, append_question


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
