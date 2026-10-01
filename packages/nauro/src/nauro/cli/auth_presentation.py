"""Present browser authentication consistently across CLI entry points."""

import contextlib
import webbrowser

import typer


def present_login_url(auth_url: str) -> None:
    typer.echo("\nOpening browser to authenticate...\n")
    typer.echo(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    with contextlib.suppress(Exception):
        webbrowser.open(auth_url)
    typer.echo("Waiting for authorization...")
