"""nauro status — Show capability table for the current project."""

from datetime import datetime, timezone

import typer

from nauro.cli.utils import resolve_target_project


def _format_time_ago(iso_timestamp: str) -> str:
    """Format an ISO timestamp as a human-readable 'N days/hours ago' string."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        if delta.days > 0:
            return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        minutes = delta.seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    except (ValueError, TypeError):
        return ""


def _count_remote_decisions(project_id: str) -> int | None:
    """Count decisions in the remote store via the manifest endpoint.

    Returns None when not authenticated, the project is not v2 cloud-mode,
    or the manifest fetch fails. The caller already renders None as
    "could not reach remote".
    """
    try:
        from nauro.cli.commands.auth import load_access_token
        from nauro.sync.hooks import _project_is_cloud

        if not load_access_token():
            return None
        if not _project_is_cloud(project_id):
            return None

        from nauro.sync.remote import PresignError, fetch_manifest

        try:
            manifest = fetch_manifest(project_id)
        except PresignError:
            return None
        return sum(
            1
            for entry in manifest
            if isinstance(entry, dict)
            and entry.get("path", "").startswith("decisions/")
            and entry.get("path", "").endswith(".md")
        )
    except Exception:
        return None


def status(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
) -> None:
    """Show which Nauro capabilities are active or inactive."""
    try:
        project_name, store_path = resolve_target_project(project)
    except SystemExit:
        typer.echo("No project found. Run 'nauro init <name>' to get started.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Project: {project_name}\n")

    # Sync — gated on auth token + v2 cloud-mode (matches hooks.py semantics).
    # ``store_path.name`` is the project_id for v2; v1 entries pass their name
    # here and silent-no-op inside _project_is_cloud.
    project_id = store_path.name
    sync_enabled = False
    try:
        from nauro.cli.commands.auth import load_access_token
        from nauro.sync.hooks import _project_is_cloud

        if load_access_token() and _project_is_cloud(project_id):
            sync_enabled = True
            typer.echo("  Sync          active (event-driven, presign)")
        elif not load_access_token():
            typer.echo("  Sync          inactive — run `nauro auth login` to enable")
        else:
            typer.echo("  Sync          inactive — this project is local-only")
    except ImportError:
        typer.echo("  Sync          inactive — run `nauro auth login` to enable")

    # MCP
    typer.echo("  MCP           active")

    # AGENTS.md
    typer.echo("  AGENTS.md     active")

    # Decision counts and sync divergence
    from nauro.store.reader import _list_decisions

    local_decisions = _list_decisions(store_path)
    local_count = len(local_decisions)

    if sync_enabled:
        remote_count = _count_remote_decisions(project_id)
        if remote_count is not None:
            if local_count == remote_count:
                typer.echo(f"\n  Decisions: {local_count} local, {remote_count} remote (in sync)")
            else:
                typer.echo(
                    f"\n  Decisions: {local_count} local, {remote_count} remote (out of sync)"
                )

            # Last sync time
            from nauro.sync.state import load_state

            sync_state = load_state(store_path)
            if sync_state.last_full_sync:
                time_ago = _format_time_ago(sync_state.last_full_sync)
                ts_display = sync_state.last_full_sync[:19].replace("T", " ") + " UTC"
                typer.echo(f"  Last sync: {ts_display} ({time_ago})")

            if local_count != remote_count:
                typer.echo("  Run `nauro sync` to reconcile.")
        else:
            typer.echo(f"\n  Decisions: {local_count} local (could not reach remote)")
    else:
        typer.echo(f"\n  Decisions: {local_count} local")
