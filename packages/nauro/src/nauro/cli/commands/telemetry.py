"""nauro telemetry — manage anonymous usage telemetry state.

Subcommands let users inspect, opt in, opt out, or rotate their analytics
identity without hand-editing ~/.nauro/config.json. NAURO_TELEMETRY=0 always
overrides config-level state at read time and is reported by ``status``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import typer

from nauro.constants import NAURO_TELEMETRY_ENV, TELEMETRY_CONSENT_VERSION
from nauro.store.config import get_telemetry_config, load_config, save_config
from nauro.telemetry import _rotate_anonymous_id

telemetry_app = typer.Typer(
    help="Manage anonymous usage telemetry.",
    no_args_is_help=True,
)


def _format_bool_or_null(value: bool | None) -> str:
    if value is None:
        return "null (not yet recorded)"
    return "true" if value else "false"


@telemetry_app.command()
def status() -> None:
    """Show current telemetry state, including any NAURO_TELEMETRY env override."""
    cfg = get_telemetry_config()
    data = load_config()
    section = data.get("telemetry") or {}
    raw_enabled = section.get("enabled")
    env_value = os.environ.get(NAURO_TELEMETRY_ENV)
    env_overrides = env_value == "0"

    typer.echo(f"anonymous_id:        {cfg.anonymous_id}")
    typer.echo(f"enabled (config):    {_format_bool_or_null(raw_enabled)}")
    typer.echo(f"enabled (effective): {_format_bool_or_null(cfg.enabled)}")
    consent_version_text = cfg.consent_version if cfg.consent_version is not None else "null"
    typer.echo(f"consent_version:     {consent_version_text}")
    typer.echo(f"consented_at:        {cfg.consented_at or 'not yet recorded'}")
    if env_value is None:
        typer.echo(f"{NAURO_TELEMETRY_ENV} override:  not set")
    else:
        typer.echo(
            f"{NAURO_TELEMETRY_ENV} override:  "
            f"{env_value!r}" + (" (disables telemetry)" if env_overrides else "")
        )


def _persist_enabled(enabled: bool) -> None:
    data = load_config()
    section = data.get("telemetry") or {}
    section["enabled"] = enabled
    section["consent_version"] = TELEMETRY_CONSENT_VERSION
    section["consented_at"] = datetime.now(UTC).isoformat()
    data["telemetry"] = section
    save_config(data)


@telemetry_app.command()
def enable() -> None:
    """Opt in to telemetry. Records consent_version and consented_at."""
    _persist_enabled(True)
    typer.echo("Telemetry enabled.")


@telemetry_app.command()
def disable() -> None:
    """Opt out of telemetry. Suppresses future events; anonymous_id is preserved."""
    _persist_enabled(False)
    typer.echo("Telemetry disabled.")


@telemetry_app.command()
def reset() -> None:
    """Rotate anonymous_id to a fresh UUID4. Consent state is preserved."""
    new_id = _rotate_anonymous_id()
    typer.echo(f"Rotated anonymous_id. New id: {new_id}")
    typer.echo("Consent state preserved (run 'nauro telemetry status' to verify).")
