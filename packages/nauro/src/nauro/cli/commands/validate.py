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
    from nauro.cli.utils import resolve_target_project
    from nauro.store.reader import _list_decisions

    project_name, store_path = resolve_target_project(project)
    decisions = _list_decisions(store_path)
    active = [d for d in decisions if d.get("status", "active") == "active"]

    typer.echo(f"Project: {project_name}")
    typer.echo(f"Total decisions: {len(decisions)}")
    typer.echo(f"Active decisions: {len(active)}")
    typer.echo("Search: BM25 (bm25s + PyStemmer, built on-the-fly per query)")
