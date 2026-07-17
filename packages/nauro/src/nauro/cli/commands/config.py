"""nauro config — inspect and remove Nauro configuration.

The store layer (``set_config`` / ``save_config``) is written by feature-
specific commands such as ``nauro auth login``. This CLI surface is read-only
plus cleanup: list / get / unset, no generic ``set``.
"""

import typer

from nauro.store.config import (
    _config_file,
    get_config,
    load_config,
    unset_config,
)

config_app = typer.Typer(help="Inspect and remove Nauro configuration.")


# Substrings that mark a config key (at any nesting depth) as sensitive. The
# `auth` block nests credentials under `access_token` / `refresh_token`, so
# matching must look inside dict values, not just top-level string keys.
_SENSITIVE_KEY_MARKERS = ("key", "token", "secret", "password", "credential")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _mask(key: str, value: object) -> object:
    """Mask sensitive values (keys/tokens) for display.

    Recurses into dicts so nested credentials — notably the bearer and refresh
    tokens under the ``auth`` block — are never printed in full. A sensitive
    string is shown as ``abcd...wxyz``; a short sensitive string is fully
    redacted; non-sensitive values pass through unchanged.
    """
    if isinstance(value, dict):
        return {k: _mask(k, v) for k, v in value.items()}
    if isinstance(value, str) and _is_sensitive_key(key):
        if len(value) > 8:
            return value[:4] + "..." + value[-4:]
        return "***"
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
        typer.echo(f"{key}: {_mask(key, value)}")


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
