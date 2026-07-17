"""Deprecated Nauro 1.x compatibility shims for retired product telemetry."""

import typer

telemetry_app = typer.Typer(
    help="Deprecated compatibility commands for retired product telemetry.",
    no_args_is_help=True,
)

_RETIRED_MESSAGE = (
    "Product telemetry has been removed. This deprecated compatibility command "
    "makes no changes and will be removed in Nauro 2.0."
)


def _report_retired() -> None:
    typer.echo(_RETIRED_MESSAGE)


@telemetry_app.command()
def status() -> None:
    """Report that product telemetry has been removed."""
    _report_retired()


@telemetry_app.command()
def enable() -> None:
    """Retained as an inert Nauro 1.x compatibility command."""
    _report_retired()


@telemetry_app.command()
def disable() -> None:
    """Retained as an inert Nauro 1.x compatibility command."""
    _report_retired()


@telemetry_app.command()
def reset() -> None:
    """Retained as an inert Nauro 1.x compatibility command."""
    _report_retired()
