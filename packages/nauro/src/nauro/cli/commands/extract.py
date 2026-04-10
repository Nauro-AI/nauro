"""nauro extract — Manually run LLM extraction on the latest git commit or session.

Resolves the current working directory to a project via the registry,
reads the latest commit info, runs the extraction pipeline, and writes
any extracted decisions/questions/state to the store.

This is what the post-commit git hook calls, and also useful for manual testing.
Supports --session flag for extracting from Claude Code session JSONL files.
"""

from __future__ import annotations

from pathlib import Path

import typer

from nauro.cli.utils import resolve_target_project
from nauro.constants import DEFAULT_SIGNAL_THRESHOLD


def extract(
    threshold: float = typer.Option(
        DEFAULT_SIGNAL_THRESHOLD,
        "--threshold",
        "-t",
        help="Minimum composite_score to write to store.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="Extract from a Claude Code session JSONL.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run extraction but print results instead of writing to the store.",
    ),
    commit: str | None = typer.Option(
        None,
        "--commit",
        help="Specific commit hash or ref to extract from.",
    ),
) -> None:
    """Run extraction on the latest commit or a Claude Code session."""
    project_name, store_path = resolve_target_project(project)

    if session:
        _extract_session(session, store_path, threshold)
    else:
        _extract_commit(store_path, threshold, dry_run=dry_run, commit_ref=commit)


def _extract_commit(
    store_path: Path,
    threshold: float,
    dry_run: bool = False,
    commit_ref: str | None = None,
) -> None:
    """Extract from a git commit."""

    from nauro.extraction.pipeline import (
        extract_from_commit,
        get_commit_info,
        process_commit,
    )
    from nauro.store import reader

    cwd = Path.cwd()

    commit_message, diff_summary, changed_files = get_commit_info(
        str(cwd),
        commit_ref=commit_ref,
    )
    if not commit_message:
        typer.echo("Error: no commits found in this repository", err=True)
        raise typer.Exit(1)

    typer.echo(f"Extracting from: {commit_message}")
    typer.echo(f"  Files changed: {len(changed_files)}")

    if dry_run:
        from nauro.extraction.types import ExtractionSkipped

        # Run extraction pipeline (Haiku call + scoring + dedup) without writing
        try:
            existing_titles = [d["title"] for d in reader.list_active_decisions(store_path)]
        except Exception:
            existing_titles = None

        outcome = extract_from_commit(
            commit_message,
            diff_summary,
            changed_files,
            existing_decisions=existing_titles,
        )

        if isinstance(outcome, ExtractionSkipped):
            typer.echo(f"  Skipped: {outcome.reason}")
            return

        result = outcome
        signal = result.signal
        typer.echo(f"  Composite score: {signal.composite_score:.3f}")
        typer.echo(f"  Skip: {result.skip}")
        typer.echo(f"  Reasoning: {signal.reasoning}")

        typer.echo(f"  Decisions: {len(result.decisions)}")
        for i, d in enumerate(result.decisions, 1):
            typer.echo(f"    [{i}] {d.get('title', 'Untitled')}")
            if d.get("rationale"):
                typer.echo(f"        Rationale: {d['rationale']}")
            if d.get("rejected"):
                for r in d["rejected"]:
                    typer.echo(f"        Rejected: {r.get('alternative')} — {r.get('reason')}")
            typer.echo(f"        Confidence: {d.get('confidence', '?')}")

        if result.questions:
            typer.echo(f"  Questions: {len(result.questions)}")
            for q in result.questions:
                typer.echo(f"    - {q}")

        if result.state_delta:
            typer.echo(f"  State delta: {result.state_delta}")

        if existing_titles:
            typer.echo(f"  Dedup: {len(existing_titles)} existing titles checked")

        would_write = "yes" if not result.skip and signal.composite_score >= threshold else "no"
        typer.echo(f"  Would write: {would_write}")
        return

    commit_result = process_commit(str(cwd), store_path, threshold=threshold)

    if commit_result is None:
        typer.echo(f"  Skipped: signal below threshold ({threshold})")
    else:
        typer.echo(f"  Composite score: {commit_result.signal.composite_score:.2f}")
        if commit_result.decisions:
            typer.echo(f"  Decisions: {len(commit_result.decisions)}")
        if commit_result.questions:
            typer.echo(f"  Questions: {len(commit_result.questions)}")
        if commit_result.state_delta:
            typer.echo(f"  State: {commit_result.state_delta}")


def _extract_session(session_id: str, store_path: Path, threshold: float) -> None:
    """Extract from a Claude Code session JSONL file."""
    # TODO: convert session_extractor to ExtractionOutcome (deferred from D63)
    from nauro.extraction.pipeline import _append_extraction_log, route_extraction_to_store
    from nauro.extraction.session_extractor import (
        extract_from_session_jsonl,
        find_session_jsonl,
    )
    from nauro.extraction.signal import from_dict

    cwd = str(Path.cwd())
    session_path = find_session_jsonl(session_id, cwd=cwd)

    if not session_path:
        typer.echo(f"Error: session file not found for '{session_id}'", err=True)
        typer.echo("Searched in ~/.claude/projects/", err=True)
        raise typer.Exit(1)

    typer.echo(f"Extracting from session: {session_path}")

    result = extract_from_session_jsonl(session_path, store_path)
    signal = from_dict(result)

    _append_extraction_log(
        store_path,
        {
            "source": "session",
            "session_id": session_id,
            "signal": signal.to_dict(),
            "composite_score": signal.composite_score,
            "skip": result.get("skip", True),
            "reasoning": signal.reasoning,
            "captured": not result.get("skip") and signal.composite_score >= threshold,
        },
    )

    if result.get("skip") or signal.composite_score < threshold:
        typer.echo(f"  Skipped: score {signal.composite_score:.2f} < {threshold}")
        return

    route_extraction_to_store(
        result,
        store_path,
        source="session",
        session_id=session_id,
        trigger=f"session extraction ({session_id})",
    )

    n_decisions = len(result.get("decisions", []))
    n_questions = len(result.get("questions", []))
    delta = result.get("state_delta")
    typer.echo(f"  Composite score: {signal.composite_score:.2f}")
    if n_decisions:
        typer.echo(f"  Decisions: {n_decisions}")
    if n_questions:
        typer.echo(f"  Questions: {n_questions}")
    if delta:
        typer.echo(f"  State: {delta}")
