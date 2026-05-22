"""nauro diff — Show changes between context snapshots."""

import typer
from nauro_core.operations import diff_since_last_session as _diff_since_last_session_op

from nauro.cli.utils import resolve_target_project
from nauro.mcp.tools import tool_diff_since_last_session
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.snapshot import list_snapshots, load_snapshot


def _parse_since(value: str) -> int:
    """Parse a --since value like '7d', '14d', or '30' into days."""
    v = value.strip()
    if v.endswith("d"):
        v = v[:-1]
    if not v.isdigit():
        raise typer.BadParameter(
            f"Invalid --since value: {value!r}. Use a number or Nd (e.g., 7d)."
        )
    return int(v)


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
    _project_name, store_path = resolve_target_project(project)

    if since is not None:
        days = _parse_since(since)
        envelope = tool_diff_since_last_session(store_path, days)
        if envelope.get("status") == "error":
            typer.echo(envelope.get("guidance", ""), err=True)
            raise typer.Exit(code=1)
        typer.echo(envelope.get("diff") or "")
        return

    if version_a is None:
        envelope = tool_diff_since_last_session(store_path, None)
        if envelope.get("status") == "error":
            typer.echo(envelope.get("guidance", ""), err=True)
            raise typer.Exit(code=1)
        typer.echo(envelope.get("diff") or "")
        return

    if version_b is None:
        snapshots = list_snapshots(store_path)
        if not snapshots:
            typer.echo("Error: no snapshots found. Run 'nauro sync' to create one", err=True)
            raise typer.Exit(code=1)
        latest_version = snapshots[0]["version"]
        if version_a == latest_version:
            typer.echo(f"Version {version_a} is already the latest snapshot.")
            return
        try:
            baseline_snap = load_snapshot(store_path, version_a)
            latest_snap = load_snapshot(store_path, latest_version)
        except FileNotFoundError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        result = _diff_since_last_session_op(
            FilesystemStore(store_path), baseline_snap, latest_snap
        )
        typer.echo(result.diff or "")
        return

    try:
        baseline_snap = load_snapshot(store_path, version_a)
        latest_snap = load_snapshot(store_path, version_b)
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    result = _diff_since_last_session_op(FilesystemStore(store_path), baseline_snap, latest_snap)
    typer.echo(result.diff or "")
