"""Nauro CLI — Typer app entry point.

This module defines the top-level Typer application and registers
all subcommands: init, note, sync, log, diff, import, serve,
config, auth, telemetry.
"""

import typer

from nauro.cli.autogen import register_autogen_commands
from nauro.telemetry import consent
from nauro.telemetry.cli_wrapper import instrument_app


def _version_callback(value: bool) -> None:
    if value:
        from nauro import __version__

        typer.echo(f"nauro {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="nauro",
    help=(
        "Set your project's doctrine once; every connected AI agent inherits it."
        "\n\nRun 'nauro telemetry --help' to manage anonymous usage telemetry."
    ),
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
    """Set your project's doctrine once; every connected AI agent inherits it."""
    consent.maybe_prompt()


def _register_commands() -> None:
    """Import and register all CLI subcommands."""
    from nauro.cli.commands import (  # noqa: F401
        adopt,
        attach,
        auth,
        config,
        hook,
        import_cmd,
        init,
        link,
        log,
        note,
        projects,
        questions,
        render_plugin,
        serve,
        setup,
        status,
        sync,
        telemetry,
        validate,
    )

    app.command(name="init")(init.init)
    app.command(name="adopt")(adopt.adopt)
    app.command(name="attach")(attach.attach)
    app.command(name="link")(link.link)
    app.command(name="note")(note.note)
    app.add_typer(projects.projects_app, name="projects")
    app.add_typer(questions.questions_app, name="questions")
    app.add_typer(hook.hook_app, name="hook")
    app.command(name="sync")(sync.sync)
    app.command(name="log")(log.log)
    app.command(name="import")(import_cmd.import_cmd)
    app.command(name="serve")(serve.serve)
    app.command(name="render-plugin", hidden=True)(render_plugin.render_plugin)
    app.add_typer(setup.setup_app, name="setup")
    app.command(name="status")(status.status)
    app.add_typer(config.config_app, name="config")
    app.add_typer(validate.validate_app, name="validate")
    app.add_typer(auth.auth_app, name="auth")
    app.add_typer(telemetry.telemetry_app, name="telemetry")


_register_commands()
register_autogen_commands(app)
instrument_app(app)

if __name__ == "__main__":
    app()
