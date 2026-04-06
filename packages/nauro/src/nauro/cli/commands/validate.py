"""nauro validate — Manage validation indexes."""

from __future__ import annotations

import typer

from nauro.cli.utils import resolve_target_project

validate_app = typer.Typer(help="Manage validation indexes.")


@validate_app.command(name="rebuild-index")
def rebuild_index(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
) -> None:
    """Rebuild the embedding index from all existing decisions."""
    project_name, store_path = resolve_target_project(project)

    from nauro.validation.tier2 import rebuild_embedding_index

    typer.echo(f"Rebuilding embedding index for {project_name}...")
    result = rebuild_embedding_index(store_path)
    typer.echo(
        f"Done. Indexed: {result['indexed']}, Failed: {result['failed']}, Model: {result['model']}"
    )


@validate_app.command(name="status")
def status(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
) -> None:
    """Show embedding index health."""
    import json

    project_name, store_path = resolve_target_project(project)

    from nauro.validation.tier2 import EMBEDDING_INDEX_FILE

    index_path = store_path / EMBEDDING_INDEX_FILE
    if not index_path.exists():
        typer.echo("No embedding index found. Run 'nauro validate rebuild-index' to create one.")
        return

    try:
        index = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        typer.echo("Embedding index is corrupted. Run 'nauro validate rebuild-index'.")
        return

    model = index.get("model", "unknown")
    decisions = index.get("decisions", {})
    total = len(decisions)
    with_embedding = sum(1 for d in decisions.values() if d.get("embedding"))

    typer.echo(f"Project: {project_name}")
    typer.echo(f"Model: {model}")
    typer.echo(f"Decisions indexed: {total}")
    typer.echo(f"With embeddings: {with_embedding}")
    typer.echo(f"Without embeddings (Jaccard fallback): {total - with_embedding}")
