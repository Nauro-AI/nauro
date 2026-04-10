"""nauro status — Show capability table for the current project."""

import os
from datetime import UTC, datetime

import typer

from nauro.cli.utils import resolve_target_project


def _format_time_ago(iso_timestamp: str) -> str:
    """Format an ISO timestamp as a human-readable 'N days/hours ago' string."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - dt
        if delta.days > 0:
            return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        minutes = delta.seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    except (ValueError, TypeError):
        return ""


def _count_remote_decisions(project_name: str) -> int | None:
    """Count decisions in the remote S3 store. Returns None on failure."""
    try:
        from nauro.sync.config import load_sync_config, s3_prefix
        from nauro.sync.remote import create_client, list_remote

        config = load_sync_config()
        if not config.enabled or not (config.user_id or config.sanitized_sub):
            return None

        client = create_client(config)
        user_key = config.user_id or config.sanitized_sub
        prefix = s3_prefix(user_key, project_name) + "decisions/"
        remote_files = list_remote(client, config.bucket_name, prefix)
        return sum(1 for f in remote_files if f["key"].endswith(".md"))
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

    # Extraction
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_key:
        from nauro.store.config import load_config

        has_key = bool(load_config().get("api_key"))

    if has_key:
        from nauro.constants import DEFAULT_EXTRACTION_MODEL, NAURO_EXTRACTION_MODEL_ENV

        model = os.environ.get(NAURO_EXTRACTION_MODEL_ENV, DEFAULT_EXTRACTION_MODEL)
        typer.echo(f"  Extraction    active ({model})")
    else:
        typer.echo("  Extraction    inactive — add API key to enable")

    # Sync
    sync_enabled = False
    try:
        from nauro.sync.config import load_sync_config

        config = load_sync_config()
        if config.enabled:
            sync_enabled = True
            typer.echo(f"  Sync          active (event-driven, S3: {config.bucket_name})")
        else:
            typer.echo("  Sync          inactive — run `nauro sync --cloud-setup` to enable")
    except ImportError:
        typer.echo("  Sync          inactive — run `nauro sync --cloud-setup` to enable")

    # MCP
    typer.echo("  MCP           active")

    # AGENTS.md
    typer.echo("  AGENTS.md     active")

    # Decision counts and sync divergence
    from nauro.store.reader import _list_decisions

    local_decisions = _list_decisions(store_path)
    local_count = len(local_decisions)

    if sync_enabled:
        remote_count = _count_remote_decisions(project_name)
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
