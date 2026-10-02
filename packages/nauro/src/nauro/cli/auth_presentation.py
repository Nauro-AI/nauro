"""Present browser authentication consistently across CLI entry points."""

import contextlib
import threading
import webbrowser

import typer


def present_login_url(auth_url: str) -> None:
    typer.echo("\nOpening browser to authenticate...\n")
    typer.echo(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    open_browser = webbrowser.open

    def launch() -> None:
        with contextlib.suppress(Exception):
            open_browser(auth_url)

    # Terminal browsers can wait until exit, so they must not block OAuth callbacks.
    with contextlib.suppress(Exception):
        threading.Thread(target=launch, name="nauro-auth-browser", daemon=True).start()
    typer.echo("Waiting for authorization...")
