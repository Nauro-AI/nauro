"""nauro status — Show capability table for the current project."""

import json
import shlex
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


def _repo_recorded_commands(repo: Path) -> list[str | None]:
    """Recorded nauro MCP commands in this repo's configs, one entry per wired config.

    Single read of ``.mcp.json`` and ``.cursor/mcp.json`` each — presence
    ("the repo is wired" iff the list is non-empty) and the recorded command
    both derive from the same parse. A wired config whose nauro entry carries a
    missing or empty command contributes ``None``: it still counts as wired,
    but there is nothing to probe. Read-only and soft-failing: a missing,
    unreadable, or malformed config contributes nothing — status must never
    crash on someone else's config file.
    """
    commands: list[str | None] = []
    for rel in (".mcp.json", ".cursor/mcp.json"):
        try:
            config = json.loads((repo / rel).read_text())
        except Exception:
            continue
        if not isinstance(config, dict):
            continue
        servers = config.get("mcpServers")
        if not isinstance(servers, dict) or "nauro" not in servers:
            continue
        entry = servers["nauro"]
        cmd = entry.get("command") if isinstance(entry, dict) else None
        commands.append(cmd if isinstance(cmd, str) and cmd else None)
    return commands


def _codex_recorded_command() -> tuple[bool, str | None]:
    """Return ``(wired, recorded command)`` for the user-global Codex config.

    Single read of ``~/.codex/config.toml``, same parse approach as setup.py.
    ``(True, None)`` means a nauro entry exists but records no usable command —
    wired for presence, nothing to probe. Any read or parse failure counts as
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
        return (False, None)
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict) or "nauro" not in servers:
        return (False, None)
    entry = servers["nauro"]
    cmd = entry.get("command") if isinstance(entry, dict) else None
    return (True, cmd if isinstance(cmd, str) and cmd else None)


def _probe_distinct_commands(
    commands: set[str], hook_commands: set[str] | None = None
) -> dict[str, bool]:
    """Probe each distinct recorded command once for liveness.

    Sequential: N repos usually share one recorded path, so the common case is
    a single short probe. ``probe_nauro_command`` soft-fails by contract, so a
    dead command costs at most its timeout, never an exception.
    """
    from nauro.cli.commands.setup import CODEX_HOOK_PROBE_ARGS

    hook_commands = hook_commands or set()
    return {
        cmd: cli_utils.probe_nauro_command(
            cmd,
            args=CODEX_HOOK_PROBE_ARGS if cmd in hook_commands else ("--version",),
        )
        for cmd in commands
    }


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


def _codex_hook_recorded_command(entry: object) -> str | None:
    """Return the Nauro executable recorded in a Codex bootstrap hook."""
    from nauro.cli.commands.setup import CODEX_HOOK_SUBCOMMAND

    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if not isinstance(command, str) or CODEX_HOOK_SUBCOMMAND not in command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token == "hook" and tokens[index : index + 2] == ["hook", "codex-bootstrap"]:
            if index > 0:
                return tokens[index - 1]
    return None


def _repo_codex_hook_state(repo: Path) -> tuple[bool, bool, list[str | None]]:
    """Return presence, structural completeness, and commands for Codex hooks."""
    from nauro.cli.commands.setup import CODEX_HOOK_EVENTS, CODEX_HOOK_SUBCOMMAND

    try:
        config = json.loads((repo / ".codex" / "hooks.json").read_text())
    except Exception:
        return (False, False, [])
    if not isinstance(config, dict):
        return (False, False, [])
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return (False, False, [])

    found_events: list[bool] = []
    commands: list[str | None] = []
    for event in CODEX_HOOK_EVENTS:
        found = False
        event_matchers = hooks.get(event)
        if isinstance(event_matchers, list):
            for matcher in event_matchers:
                if not isinstance(matcher, dict):
                    continue
                entries = matcher.get("hooks")
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    fields = (entry.get("command"), entry.get("commandWindows"))
                    if not any(
                        isinstance(value, str) and CODEX_HOOK_SUBCOMMAND in value
                        for value in fields
                    ):
                        continue
                    found = True
                    commands.append(_codex_hook_recorded_command(entry))
        found_events.append(found)
    return (any(found_events), all(found_events), commands)


def status(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
    no_probe: bool = typer.Option(
        False,
        "--no-probe",
        help="Skip executable liveness probes; report wiring presence only.",
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

    # Each wiring config is read once: presence and the recorded command come
    # from the same parse.
    try:
        repo_commands = [_repo_recorded_commands(repo) for repo in repo_paths]
    except Exception:
        repo_commands = []
    mcp_wired = sum(1 for cmds in repo_commands if cmds)
    try:
        codex_global, codex_command = _codex_recorded_command()
    except Exception:
        codex_global, codex_command = False, None
    try:
        codex_hook_states = [_repo_codex_hook_state(repo) for repo in repo_paths]
    except Exception:
        codex_hook_states = []
    configured_states = [state for state in codex_hook_states if state[0]]

    recorded_commands = {cmd for cmds in repo_commands for cmd in cmds if cmd}
    if codex_command:
        recorded_commands.add(codex_command)
    hook_recorded_commands = {
        command
        for _present, _complete, commands in configured_states
        for command in commands
        if command
    }
    recorded_commands.update(hook_recorded_commands)
    probe_results: dict[str, bool] | None = None
    if not no_probe and recorded_commands:
        try:
            probe_results = _probe_distinct_commands(recorded_commands, hook_recorded_commands)
        except Exception:
            probe_results = None

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
            mcp_commands = {cmd for cmds in repo_commands for cmd in cmds if cmd}
            if codex_command:
                mcp_commands.add(codex_command)
            if probe_results is not None and mcp_commands:
                healthy = all(probe_results.get(command, True) for command in mcp_commands)

        if healthy:
            typer.echo(f"  MCP           active ({detail_str})")
        else:
            typer.echo(
                f"  MCP           BROKEN — {detail_str} but the recorded command "
                "won't run; re-run 'nauro setup all'"
            )
    else:
        typer.echo("  MCP           inactive — run 'nauro setup all'")

    if configured_states:
        hook_detail = f"wired in {len(configured_states)}/{len(repo_paths)} repos"
        structurally_complete = all(
            complete and commands and all(command for command in commands)
            for _present, complete, commands in configured_states
        )
        if not structurally_complete:
            typer.echo(
                f"  Codex hooks   BROKEN - {hook_detail} but the lifecycle wiring "
                "is incomplete; re-run 'nauro setup all --with-hooks'"
            )
        elif no_probe:
            typer.echo(f"  Codex hooks   configured ({hook_detail}; liveness not probed)")
        elif probe_results is None:
            typer.echo(f"  Codex hooks   configured ({hook_detail}; liveness unknown)")
        else:
            hook_commands = {
                command
                for _present, _complete, commands in configured_states
                for command in commands
                if command
            }
            hook_command_healthy = all(
                probe_results.get(command, True) for command in hook_commands
            )
            if hook_command_healthy:
                typer.echo(f"  Codex hooks   configured ({hook_detail}; command healthy)")
            else:
                typer.echo(
                    f"  Codex hooks   BROKEN - {hook_detail} but the recorded command "
                    "won't run; re-run 'nauro setup all --with-hooks'"
                )
    else:
        typer.echo("  Codex hooks   inactive - run 'nauro setup codex --with-hooks'")

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
