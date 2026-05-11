"""nauro config — inspect and remove Nauro configuration.

The store layer (``set_config`` / ``save_config``) is written by feature-
specific commands (``nauro auth login``, ``nauro sync --cloud-setup``,
``nauro telemetry enable/disable``). This CLI surface is read-only +
cleanup: list / get / unset, no generic ``set``.
"""

import typer

from nauro.store.config import (
    _config_file,
    get_config,
    load_config,
    unset_config,
)

config_app = typer.Typer(help="Inspect and remove Nauro configuration.")


def _mask(key: str, value: str) -> str:
    """Mask sensitive values (keys/tokens) for display."""
    if "key" in key.lower() and len(value) > 8:
        return value[:4] + "..." + value[-4:]
    return value


@config_app.command(name="get")
def config_get(
    key: str = typer.Argument(help="Config key to look up"),
) -> None:
    """Get a configuration value."""
    value = get_config(key)
    if value is None:
        typer.echo(f"{key}: (not set)")
        raise typer.Exit(code=1)
    typer.echo(f"{key}: {_mask(key, value)}")


@config_app.command(name="list")
def config_list() -> None:
    """Show all configuration values."""
    data = load_config()
    if not data:
        typer.echo("No configuration set.")
        return
    for key, value in sorted(data.items()):
        display = _mask(key, value) if isinstance(value, str) else value
        typer.echo(f"{key}: {display}")


@config_app.command(name="unset")
def config_unset(
    key: str = typer.Argument(help="Config key to remove"),
) -> None:
    """Remove a configuration value."""
    if unset_config(key):
        typer.echo(f"Removed {key}")
        typer.echo(f"  Config: {_config_file()}")
    else:
        typer.echo(f"{key}: (not set)")
        raise typer.Exit(code=1)
