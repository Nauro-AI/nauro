"""nauro diff — Show changes between context snapshots."""

import re

import typer

from nauro.cli.utils import resolve_target_project
from nauro.store.reader import diff_since_last_session, diff_snapshots
from nauro.store.snapshot import list_snapshots


def _parse_since(value: str) -> int:
    """Parse a --since value like '7d', '14d', or '30' into days."""
    m = re.match(r"^(\d+)d?$", value.strip())
    if not m:
        raise typer.BadParameter(
            f"Invalid --since value: {value!r}. Use a number or Nd (e.g., 7d)."
        )
    return int(m.group(1))


def diff(
    version_a: int | None = typer.Argument(
        None,
        help="First snapshot version (or only version to diff against latest).",
    ),
    version_b: int | None = typer.Argument(None, help="Second snapshot version."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Time-based diff: number of days to look back (e.g., 7d, 14d, 30).",
    ),
) -> None:
    """Show what changed between snapshots.

    No arguments: diff between last two snapshots.
    One argument: diff between that version and the latest.
    Two arguments: diff between the two specified versions.
    --since Nd: diff between the nearest snapshot to N days ago and the latest.
    """
    project_name, store_path = resolve_target_project(project)

    if since is not None:
        days = _parse_since(since)
        result = diff_since_last_session(store_path, days=days)
        typer.echo(result)
        return

    if version_a is None:
        # No args: diff since last session
        result = diff_since_last_session(store_path)
        typer.echo(result)
        return

    if version_b is None:
        # One arg: diff between version_a and latest
        snapshots = list_snapshots(store_path)
        if not snapshots:
            typer.echo("Error: no snapshots found. Run 'nauro sync' to create one", err=True)
            raise typer.Exit(code=1)
        latest = snapshots[0]["version"]
        if version_a == latest:
            typer.echo(f"Version {version_a} is already the latest snapshot.")
            return
        try:
            result = diff_snapshots(store_path, version_a, latest)
        except FileNotFoundError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1)
        typer.echo(result)
        return

    # Two args: diff between version_a and version_b
    try:
        result = diff_snapshots(store_path, version_a, version_b)
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(result)
