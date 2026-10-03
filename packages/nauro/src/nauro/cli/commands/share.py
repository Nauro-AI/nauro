"""Immutable context briefs through an attached generation replica."""

import typer
from nauro_core.mcp_tools import SHARE_CONTEXT

from nauro.cli.autogen import OutputFormat
from nauro.cli.generation_writes import with_write_options
from nauro.cli.utils import resolve_target_project

_FORMAT_OPTION = typer.Option(OutputFormat.json, "--format")


def _share_context(
    slug: str = typer.Argument(..., help="Permanent brief slug."),
    content: str = typer.Argument(..., help="Complete immutable brief body."),
    pointer_kind: str | None = typer.Option(None, help="brief, resume, or selection."),
    summary: str | None = typer.Option(None, help="Single-line discovery summary."),
    project: str | None = typer.Option(None, "--project", "-p"),
    output_format: OutputFormat = _FORMAT_OPTION,
    json_output: bool = typer.Option(False, "--json/--no-json"),
) -> None:
    """Publish an immutable brief and discovery pointer on a generation replica."""
    resolve_target_project(project)
    typer.echo("Error: share-context requires a generation replica.", err=True)
    raise typer.Exit(1)


share_context = with_write_options(_share_context, SHARE_CONTEXT)
