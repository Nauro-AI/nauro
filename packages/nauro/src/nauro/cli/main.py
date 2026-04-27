"""Nauro CLI — Typer app entry point.

This module defines the top-level Typer application and registers
all subcommands: init, note, sync, log, diff, import, extract, hook, serve, config, auth.
"""

import typer

from nauro.store.config import apply_config_to_env


def _version_callback(value: bool) -> None:
    if value:
        from nauro import __version__

        typer.echo(f"nauro {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="nauro",
    help="Local CLI for managing versioned project context for AI coding agents.",
    no_args_is_help=True,
)


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Local CLI for managing versioned project context for AI coding agents."""


def _register_commands() -> None:
    """Import and register all CLI subcommands."""
    from nauro.cli.commands import (  # noqa: F401
        attach,
        auth,
        config,
        diff,
        extract,
        hook,
        import_cmd,
        init,
        link,
        log,
        note,
        serve,
        setup,
        status,
        sync,
        validate,
    )

    app.command(name="init")(init.init)
    app.command(name="attach")(attach.attach)
    app.command(name="link")(link.link)
    app.command(name="note")(note.note)
    app.command(name="sync")(sync.sync)
    app.command(name="log")(log.log)
    app.command(name="diff")(diff.diff)
    app.command(name="import")(import_cmd.import_cmd)
    app.command(name="extract")(extract.extract)
    app.command(name="serve")(serve.serve)
    app.add_typer(hook.hook_app, name="hook")
    app.add_typer(setup.setup_app, name="setup")
    app.command(name="status")(status.status)
    app.add_typer(config.config_app, name="config")
    app.add_typer(validate.validate_app, name="validate")
    app.add_typer(auth.auth_app, name="auth")


_register_commands()
apply_config_to_env()

if __name__ == "__main__":
    app()
