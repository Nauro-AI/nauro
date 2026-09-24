"""Refuse legacy CLI mutations when local replica controls exist."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from nauro.store.migration_admission import migration_write_guard, require_migration_admission


def require_legacy_write(store_path: Path, command: str) -> None:
    try:
        require_migration_admission(store_path)
    except PermissionError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
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


@contextmanager
def legacy_write_guard(store_path: Path, command: str) -> Iterator[None]:
    with migration_write_guard(store_path):
        require_legacy_write(store_path, command)
        yield
