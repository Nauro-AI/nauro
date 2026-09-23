"""Refuse legacy CLI mutations when local replica controls exist."""

from pathlib import Path

import typer


def require_legacy_write(store_path: Path, command: str) -> None:
    # Incomplete replica evidence must not reopen the legacy writer.
    try:
        (store_path / ".replica").lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        typer.echo("Error: Cannot verify project write authority.", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Error: nauro {command} is unavailable for generation replicas.", err=True)
    raise typer.Exit(1)
