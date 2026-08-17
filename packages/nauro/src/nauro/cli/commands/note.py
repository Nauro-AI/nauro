"""nauro note — Add a decision or question to the project store."""

import typer
from nauro_core.constants import DECISIONS_DIR, OPEN_QUESTIONS_MD
from nauro_core.decision_model import DecisionConfidence
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations.propose_decision import _write_decision_direct

from nauro.cli.utils import cli_origin, resolve_target_project
from nauro.store.decision_lock import decision_write_lock
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.journal import record_event
from nauro.store.post_commit import run_post_commit
from nauro.store.store_lock import store_write_lock


def _validate_confidence(value: str) -> str:
    """Reject an invalid ``--confidence`` with a usage error instead of a traceback.
    The option stays a plain string so the introspected CLI contract stays ``text``; the enum is
    the source of truth for the accepted set.
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
        help="Force treating as a question. Takes precedence over --decision.",
    ),
    decision: bool = typer.Option(
        False,
        "--decision",
        "-d",
        help=(
            "Force treating as a decision (default). Only disables the "
            "trailing-'?' question autodetect; --question wins when both "
            "are passed."
        ),
    ),
    rationale: str | None = typer.Option(
        None,
        "--rationale",
        "-r",
        help="Why this decision was made (decisions only; ignored for questions).",
    ),
    confidence: str = typer.Option(
        "medium",
        "--confidence",
        "-c",
        help="Confidence: high, medium, low (decisions only; ignored for questions).",
        callback=_validate_confidence,
    ),
) -> None:
    """Record a decision or question in the project store.
    Decisions land in decisions/NNN-title.md, questions append to open-questions.md, and every
    note regenerates AGENTS.md in all associated repos.
    """
    if not text.strip():
        typer.echo("Note text cannot be empty.", err=True)
        raise typer.Exit(1)

    if question and decision:
        typer.echo(
            "Warning: --question and --decision were both passed; --question wins.",
            err=True,
        )

    project_name, store_path = resolve_target_project(project)
    fs_store = FilesystemStore(store_path)

    is_question = question or (text.rstrip().endswith("?") and not decision)

    if is_question:
        # Explicit `-c medium` is indistinguishable from the default, so it
        # cannot trigger the warning; that trade-off is accepted.
        if rationale is not None or confidence != "medium":
            typer.echo(
                "Warning: --rationale/--confidence apply to decisions only; "
                "ignored for this question.",
                err=True,
            )
        # Hold the lock across the read-mint-insert-write append so concurrent
        # local writers cannot read the same open-questions.md pre-image and
        # clobber one another's entry. Mirrors the decision branch below, which
        # already wraps decision_write_lock. AGENTS.md regen stays outside.
        with store_write_lock(store_path, OPEN_QUESTIONS_MD):
            _flag_question_op(fs_store, text, None)
        record_event(
            store_path,
            operation="flag_question",
            target=OPEN_QUESTIONS_MD,
            status="committed",
            payload={"question": text},
            origin_factory=cli_origin,
        )
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
        record_event(
            store_path,
            operation="propose_decision",
            target=DECISIONS_DIR,
            status="committed",
            payload={"title": text, "rationale": rationale, "confidence": confidence},
            origin_factory=cli_origin,
            decision_id=decision_id,
        )
        filepath = store_path / DECISIONS_DIR / f"{decision_id}.md"
        typer.echo(f"Decision recorded in {project_name}:")
        typer.echo(f"  {filepath}")

    # Refresh AGENTS.md so MCP-disconnected agents see the update without
    # requiring a separate `nauro sync`. The write above has already committed,
    # so this trails it through the fail-open seam.
    outcome = run_post_commit(
        store_path,
        regenerate_agents_md=True,
        warn=lambda msg: typer.echo(msg, err=True),
    )
    for line in outcome.warnings:
        typer.echo(line, err=True)
    for repo_path in outcome.updated_repos:
        typer.echo(f"  Updated AGENTS.md: {repo_path}")
