"""nauro note — Add a decision or question to the project store."""

import typer
from nauro_core.constants import DECISIONS_DIR, OPEN_QUESTIONS_MD
from nauro_core.decision_model import DecisionConfidence
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations.propose_decision import _write_decision_direct

from nauro.cli.utils import resolve_target_project
from nauro.store.decision_lock import decision_write_lock
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.store_lock import store_write_lock
from nauro.templates.agents_md_regen import warn_then_regen


def _validate_confidence(value: str) -> str:
    """Reject an invalid ``--confidence`` at parse time with a clean exit.

    Kept as a string option (not a Typer enum) so the introspected CLI
    contract stays ``text`` under the frozen public surface; the enum is the
    source of truth for the accepted set. Without this, an unknown value
    reaches ``DecisionConfidence(...)`` downstream and surfaces as a raw
    ``ValueError`` traceback rather than a usage error.
    """
    try:
        DecisionConfidence(value)
    except ValueError as exc:
        choices = ", ".join(c.value for c in DecisionConfidence)
        raise typer.BadParameter(f"{value!r} is not one of {choices}.") from exc
    return value


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
        callback=_validate_confidence,
    ),
) -> None:
    """Record a decision or question in the project store."""
    if not text.strip():
        typer.echo("Note text cannot be empty.", err=True)
        raise typer.Exit(1)

    project_name, store_path = resolve_target_project(project)
    fs_store = FilesystemStore(store_path)

    is_question = question or (text.rstrip().endswith("?") and not decision)

    if is_question:
        # Hold the lock across the read-mint-insert-write append so concurrent
        # local writers cannot read the same open-questions.md pre-image and
        # clobber one another's entry. Mirrors the decision branch below, which
        # already wraps decision_write_lock. AGENTS.md regen stays outside.
        with store_write_lock(store_path, OPEN_QUESTIONS_MD):
            _flag_question_op(fs_store, text, None)
        typer.echo(f"Question added to {project_name}:")
        typer.echo(f"  {text}")
        typer.echo(f"  File: {store_path / 'open-questions.md'}")
    else:
        # Hold the allocation lock across the number computation and the write
        # so concurrent local writers cannot mint the same decision number.
        # AGENTS.md regen below stays outside the lock.
        with decision_write_lock(store_path):
            decision_id = _write_decision_direct(
                fs_store,
                {
                    "title": text,
                    "rationale": rationale,
                    "confidence": confidence,
                },
            )
        filepath = store_path / DECISIONS_DIR / f"{decision_id}.md"
        typer.echo(f"Decision recorded in {project_name}:")
        typer.echo(f"  {filepath}")

    # Refresh AGENTS.md so MCP-disconnected agents see the update without
    # requiring a separate `nauro sync`. Mirrors the warn-then-regen
    # sequence in `nauro sync`.
    project_key = store_path.name
    updated_repos = warn_then_regen(
        project_key,
        store_path,
        warn=lambda msg: typer.echo(msg, err=True),
    )
    for repo_path in updated_repos:
        typer.echo(f"  Updated AGENTS.md: {repo_path}")
