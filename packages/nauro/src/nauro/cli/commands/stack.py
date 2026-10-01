"""Stack replacement through an attached generation replica."""

import typer
from nauro_core.mcp_tools import UPDATE_STACK

from nauro.cli.autogen import OutputFormat
from nauro.cli.generation_writes import with_write_options
from nauro.cli.utils import resolve_target_project

_FORMAT_OPTION = typer.Option(OutputFormat.json, "--format")


def _update_stack(
    content: str = typer.Argument(..., help="Full replacement stack document."),
    project: str | None = typer.Option(None, "--project", "-p"),
    output_format: OutputFormat = _FORMAT_OPTION,
    json_output: bool = typer.Option(False, "--json/--no-json"),
) -> None:
    resolve_target_project(project)
    typer.echo("Error: update-stack requires a generation replica.", err=True)
    raise typer.Exit(1)


update_stack = with_write_options(_update_stack, UPDATE_STACK)
