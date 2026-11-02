"""nauro validate — Validation diagnostics."""

from __future__ import annotations

import typer

validate_app = typer.Typer(help="Validation diagnostics.")


@validate_app.command(name="status")
def status(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
) -> None:
    """Show validation search status."""
    from nauro_core.decision_model import DecisionStatus
    from nauro_core.operations.decision_lookup import scan_decisions

    from nauro.cli.utils import resolve_target_project, warn_unreadable_decisions
    from nauro.store.filesystem_store import FilesystemStore

    project_name, store_path = resolve_target_project(project)
    decisions, decision_failures = scan_decisions(FilesystemStore(store_path))
    warn_unreadable_decisions(decision_failures)
    active = [d for d in decisions if d.status is DecisionStatus.active]

    typer.echo(f"Project: {project_name}")
    typer.echo(f"Total decisions: {len(decisions)}")
    typer.echo(f"Active decisions: {len(active)}")
    typer.echo("Search: BM25 (bm25s + PyStemmer, built on-the-fly per query)")
