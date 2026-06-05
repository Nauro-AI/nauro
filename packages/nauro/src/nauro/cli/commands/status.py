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

    Returns None when the manifest fetch fails. Callers must gate on
    auth + cloud-mode before invoking this — the function does not
    re-check, and a failed call against the wrong endpoint is the
    caller's bug.
    """
    try:
        from nauro.sync.remote import PresignError, fetch_manifest

        manifest = fetch_manifest(project_id)
    except PresignError:
        return None
    except Exception:
        return None
    return sum(
        1
        for entry in manifest
        if isinstance(entry, dict)
        and entry.get("path", "").startswith("decisions/")
        and entry.get("path", "").endswith(".md")
    )


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
    except typer.Exit as exc:
        typer.echo("No project found. Run 'nauro init <name>' to get started.", err=True)
        raise typer.Exit(exc.exit_code) from exc

    typer.echo(f"Project: {project_name}")
    # Surface the absolute store path. The store lives at ~/.nauro/projects/<id>/,
    # outside any repo, and no other command prints it — an agent following the
    # nauro-handoff / nauro-context skills needs it to resolve where to write
    # handoffs/<slug>.md or context/<slug>.md.
    typer.echo(f"Store:   {store_path}\n")

    # Sync — gated on auth token + v2 cloud-mode (matches hooks.py semantics).
    # ``store_path.name`` is the project_id for v2; v1 entries pass their name
    # here and silent-no-op inside is_cloud_project.
    project_id = store_path.name
    from nauro.cli.commands.auth import load_access_token
    from nauro.store.registry import is_cloud_project

    has_token = bool(load_access_token())
    is_cloud = is_cloud_project(project_id)
    sync_enabled = has_token and is_cloud
    if sync_enabled:
        typer.echo("  Sync          active (event-driven, presign)")
    elif not is_cloud:
        typer.echo(
            "  Sync          inactive — local-only project."
            " Enable with `nauro auth login`, then `nauro link --cloud`."
        )
    else:
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
