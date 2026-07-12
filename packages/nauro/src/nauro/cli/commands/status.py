"""nauro status — Show capability table for the current project."""

from datetime import datetime, timezone
from pathlib import Path

import typer

from nauro.cli import utils as cli_utils
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


def _codex_config_path() -> Path:
    """User-global Codex config path (same location setup.py writes)."""
    return Path.home() / ".codex" / "config.toml"


def _repo_has_mcp_wiring(repo: Path) -> bool:
    """True when the repo declares a nauro MCP server in .mcp.json or .cursor/mcp.json.

    Read-only probe: a missing, unreadable, or malformed config counts as
    not wired — status must never crash on someone else's config file.
    """
    import json

    for rel in (".mcp.json", ".cursor/mcp.json"):
        try:
            config = json.loads((repo / rel).read_text())
        except Exception:
            continue
        if not isinstance(config, dict):
            continue
        servers = config.get("mcpServers")
        if isinstance(servers, dict) and "nauro" in servers:
            return True
    return False


def _codex_global_wired() -> bool:
    """True when the user-global Codex config declares a nauro MCP server.

    Same parse approach as setup.py; any read or parse failure counts as
    not wired.
    """
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    try:
        with _codex_config_path().open("rb") as f:
            config = tomllib.load(f)
    except Exception:
        return False
    servers = config.get("mcp_servers")
    return isinstance(servers, dict) and "nauro" in servers


def _recorded_repo_commands(repo: Path) -> list[str]:
    """Return the nauro MCP command strings recorded in a repo's configs.

    Reads ``.mcp.json`` and ``.cursor/mcp.json`` and extracts
    ``mcpServers.nauro.command`` from each. Read-only and soft-failing: a
    missing, unreadable, malformed, or command-less config contributes nothing.
    """
    import json

    commands: list[str] = []
    for rel in (".mcp.json", ".cursor/mcp.json"):
        try:
            config = json.loads((repo / rel).read_text())
        except Exception:
            continue
        if not isinstance(config, dict):
            continue
        servers = config.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        entry = servers.get("nauro")
        if isinstance(entry, dict):
            cmd = entry.get("command")
            if isinstance(cmd, str) and cmd:
                commands.append(cmd)
    return commands


def _recorded_codex_command() -> str | None:
    """Return the nauro command recorded in the user-global Codex config, if any.

    Same parse approach as ``_codex_global_wired``; any read or parse failure
    yields None.
    """
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    try:
        with _codex_config_path().open("rb") as f:
            config = tomllib.load(f)
    except Exception:
        return None
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get("nauro")
    if isinstance(entry, dict):
        cmd = entry.get("command")
        if isinstance(cmd, str) and cmd:
            return cmd
    return None


def _safe_probe(cmd: str) -> bool:
    """Liveness probe wrapper that never raises (status soft-fail contract)."""
    try:
        return cli_utils.probe_nauro_command(cmd)
    except Exception:
        return False


def _probe_distinct_commands(commands: set[str]) -> dict[str, bool]:
    """Probe each distinct recorded command once for liveness.

    Sequential for a single command — the common case, where N repos share one
    recorded path. A small bounded thread pool only when several distinct
    commands exist, so their probes overlap instead of summing their timeouts.
    """
    distinct = list(commands)
    if not distinct:
        return {}
    if len(distinct) == 1:
        return {distinct[0]: _safe_probe(distinct[0])}
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(4, len(distinct))) as pool:
        return dict(zip(distinct, pool.map(_safe_probe, distinct)))


def _repo_has_generated_agents_md(repo: Path) -> bool:
    """True when the repo's AGENTS.md carries the Nauro generation footer.

    A file without the footer is hand-written (or stale beyond recognition)
    and counts as not generated. Unreadable files count as not generated.
    """
    from nauro.templates.agents_md import FOOTER_MARKER

    try:
        return FOOTER_MARKER in (repo / "AGENTS.md").read_text()
    except Exception:
        return False


def status(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
    no_probe: bool = typer.Option(
        False,
        "--no-probe",
        help="Skip the MCP liveness probe; report wiring presence only.",
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
    # nauro-context skill needs it to resolve where to write context/<slug>.md.
    typer.echo(f"Store:   {store_path}\n")

    # Warn when another local project shares this name — a separate store that
    # shares no decisions, usually an accidental fork. Broadly guarded so this
    # status nicety can never break the command; a v1 registry yields [] from
    # the helper, so the check simply no-ops there.
    current_id = store_path.name
    try:
        from nauro.store.registry import find_projects_by_name_v2

        shared = [pid for pid, _ in find_projects_by_name_v2(project_name) if pid != current_id]
    except Exception:
        shared = []
    if shared:
        typer.echo(
            f"  Warning: {len(shared)} other local project(s) share the name "
            f"'{project_name}'. They are separate stores — run `nauro projects` to inspect.\n",
            err=True,
        )

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
            " Enable with 'nauro auth login', then 'nauro link --cloud'."
        )
    else:
        typer.echo("  Sync          inactive — run 'nauro auth login' to enable")

    # MCP + AGENTS.md — computed from on-disk wiring, never assumed. Every
    # probe is guarded (same rationale as the shared-name check above): a
    # corrupt config or unreadable file counts as not wired, never a crash.
    try:
        from nauro.store.registry import get_repo_paths

        repo_paths = [Path(p) for p in get_repo_paths(project_id)]
    except Exception:
        repo_paths = []

    try:
        mcp_wired = sum(1 for repo in repo_paths if _repo_has_mcp_wiring(repo))
    except Exception:
        mcp_wired = 0
    try:
        codex_global = _codex_global_wired()
    except Exception:
        codex_global = False

    if mcp_wired or codex_global:
        details = []
        if repo_paths:
            details.append(f"wired in {mcp_wired}/{len(repo_paths)} repos")
        if codex_global:
            details.append("Codex global")
        detail_str = "; ".join(details)

        # Liveness: wiring can point at a nauro that no longer runs (a rebuilt or
        # corrupted project venv). Probe the distinct recorded commands so a
        # wired-but-dead install renders BROKEN instead of a false-green active.
        healthy = True
        if not no_probe:
            try:
                commands: set[str] = set()
                for repo in repo_paths:
                    commands.update(_recorded_repo_commands(repo))
                codex_cmd = _recorded_codex_command()
                if codex_cmd:
                    commands.add(codex_cmd)
                if commands:
                    healthy = all(_probe_distinct_commands(commands).values())
            except Exception:
                # A probe error must never crash status or flip a live wiring to
                # BROKEN; fall back to presence-only reporting.
                healthy = True

        if healthy:
            typer.echo(f"  MCP           active ({detail_str})")
        else:
            typer.echo(
                f"  MCP           BROKEN — {detail_str} but the recorded command "
                "won't run; re-run 'nauro setup all'"
            )
    else:
        typer.echo("  MCP           inactive — run 'nauro setup all'")

    try:
        agents_generated = sum(1 for repo in repo_paths if _repo_has_generated_agents_md(repo))
    except Exception:
        agents_generated = 0

    if agents_generated:
        typer.echo(f"  AGENTS.md     active ({agents_generated}/{len(repo_paths)} repos)")
    else:
        typer.echo("  AGENTS.md     inactive — run 'nauro sync'")

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
