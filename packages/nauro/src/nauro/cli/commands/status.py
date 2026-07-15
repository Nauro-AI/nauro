"""nauro status — Show capability table for the current project."""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer

from nauro.cli import nauro_command
from nauro.cli._codex_hooks import (
    _CODEX_HOOK_PROBE_ARGS,
    _CodexHookState,
    _inspect_codex_hooks,
    _parse_codex_hooks,
)
from nauro.cli.utils import resolve_target_project


def _is_windows() -> bool:
    return os.name == "nt"


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
            config = json.loads((repo / rel).read_text(encoding="utf-8"))
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
    commands: set[str], *, args: tuple[str, ...] = ("--version",)
) -> dict[str, bool]:
    """Probe each distinct recorded command once for liveness.

    Sequential: N repos usually share one recorded path, so the common case is
    a single short probe. ``probe_nauro_command`` soft-fails by contract, so a
    dead command costs at most its timeout, never an exception.
    """
    return {cmd: nauro_command.probe_nauro_command(cmd, args=args) for cmd in commands}


def _repo_has_generated_agents_md(repo: Path) -> bool:
    """True when the repo's AGENTS.md carries the Nauro generation footer.

    A file without the footer is hand-written (or stale beyond recognition)
    and counts as not generated. Unreadable files count as not generated.
    """
    from nauro.templates.agents_md import FOOTER_MARKER

    try:
        return FOOTER_MARKER in (repo / "AGENTS.md").read_text(encoding="utf-8")
    except Exception:
        return False


def _repo_codex_hook_state(repo: Path) -> _CodexHookState:
    """Return presence, structural completeness, and commands for Codex hooks."""
    try:
        text = (repo / ".codex" / "hooks.json").read_text(encoding="utf-8")
        config = _parse_codex_hooks(text)
    except Exception:
        return _CodexHookState(False, False, ())
    return _inspect_codex_hooks(config, windows=_is_windows())


@dataclass(frozen=True)
class _WiringSnapshot:
    repo_count: int
    mcp_wired: int
    codex_global: bool
    mcp_commands: frozenset[str]
    hook_states: tuple[_CodexHookState, ...]
    agents_generated: int

    @property
    def configured_hooks(self) -> tuple[_CodexHookState, ...]:
        return tuple(state for state in self.hook_states if state.present)

    @property
    def hook_commands(self) -> frozenset[str]:
        return frozenset(
            command
            for state in self.configured_hooks
            for command in state.recorded_commands
            if command
        )


@dataclass(frozen=True)
class _WiringProbeResults:
    skipped: bool
    mcp: dict[str, bool] | None
    hooks: dict[str, bool] | None


def _collect_wiring(repo_paths: list[Path]) -> _WiringSnapshot:
    try:
        repo_commands = [_repo_recorded_commands(repo) for repo in repo_paths]
    except Exception:
        repo_commands = []
    try:
        codex_global, codex_command = _codex_recorded_command()
    except Exception:
        codex_global, codex_command = False, None
    try:
        hook_states = tuple(_repo_codex_hook_state(repo) for repo in repo_paths)
    except Exception:
        hook_states = ()
    try:
        agents_generated = sum(1 for repo in repo_paths if _repo_has_generated_agents_md(repo))
    except Exception:
        agents_generated = 0

    mcp_commands = {command for commands in repo_commands for command in commands if command}
    if codex_command:
        mcp_commands.add(codex_command)
    return _WiringSnapshot(
        repo_count=len(repo_paths),
        mcp_wired=sum(1 for commands in repo_commands if commands),
        codex_global=codex_global,
        mcp_commands=frozenset(mcp_commands),
        hook_states=hook_states,
        agents_generated=agents_generated,
    )


def _probe_wiring(snapshot: _WiringSnapshot, *, no_probe: bool) -> _WiringProbeResults:
    if no_probe:
        return _WiringProbeResults(True, None, None)
    mcp_results = _probe_commands(snapshot.mcp_commands, args=("--version",))
    hook_results = _probe_commands(snapshot.hook_commands, args=_CODEX_HOOK_PROBE_ARGS)
    return _WiringProbeResults(False, mcp_results, hook_results)


def _probe_commands(
    commands: frozenset[str],
    *,
    args: tuple[str, ...],
) -> dict[str, bool] | None:
    if not commands:
        return None
    try:
        return _probe_distinct_commands(set(commands), args=args)
    except Exception:
        return None


def _mcp_status_line(snapshot: _WiringSnapshot, probes: _WiringProbeResults) -> str:
    if not snapshot.mcp_wired and not snapshot.codex_global:
        return "  MCP           inactive - run 'nauro setup all'"
    details = []
    if snapshot.repo_count:
        details.append(f"wired in {snapshot.mcp_wired}/{snapshot.repo_count} repos")
    if snapshot.codex_global:
        details.append("Codex global")
    detail = "; ".join(details)
    healthy = probes.mcp is None or all(
        probes.mcp.get(command, True) for command in snapshot.mcp_commands
    )
    if healthy:
        return f"  MCP           active ({detail})"
    return (
        f"  MCP           BROKEN - {detail} but the recorded command won't run; "
        "re-run 'nauro setup all'"
    )


def _codex_hooks_status_line(snapshot: _WiringSnapshot, probes: _WiringProbeResults) -> str:
    configured = snapshot.configured_hooks
    if not configured:
        return "  Codex hooks   inactive - run 'nauro setup codex --with-hooks'"
    detail = f"wired in {len(configured)}/{snapshot.repo_count} repos"
    complete = all(
        state.complete and state.recorded_commands and all(state.recorded_commands)
        for state in configured
    )
    if not complete:
        return (
            f"  Codex hooks   BROKEN - {detail} but the lifecycle wiring is incomplete; "
            "re-run 'nauro setup all --with-hooks'"
        )
    if probes.skipped:
        return f"  Codex hooks   configured ({detail}; liveness not probed)"
    if probes.hooks is None:
        return f"  Codex hooks   configured ({detail}; liveness unknown)"
    healthy = all(probes.hooks.get(command, True) for command in snapshot.hook_commands)
    if healthy:
        return f"  Codex hooks   configured ({detail}; command healthy)"
    return (
        f"  Codex hooks   BROKEN - {detail} but the recorded command won't run; "
        "re-run 'nauro setup all --with-hooks'"
    )


def _agents_status_line(snapshot: _WiringSnapshot) -> str:
    if snapshot.agents_generated:
        return f"  AGENTS.md     active ({snapshot.agents_generated}/{snapshot.repo_count} repos)"
    return "  AGENTS.md     inactive - run 'nauro sync'"


def _render_wiring_status(repo_paths: list[Path], *, no_probe: bool) -> None:
    snapshot = _collect_wiring(repo_paths)
    probes = _probe_wiring(snapshot, no_probe=no_probe)
    typer.echo(_mcp_status_line(snapshot, probes))
    typer.echo(_codex_hooks_status_line(snapshot, probes))
    typer.echo(_agents_status_line(snapshot))


def _warn_if_project_name_shared(project_name: str, project_id: str) -> None:
    try:
        from nauro.store.registry import find_projects_by_name_v2

        shared = [
            candidate_id
            for candidate_id, _ in find_projects_by_name_v2(project_name)
            if candidate_id != project_id
        ]
    except Exception:
        shared = []
    if shared:
        typer.echo(
            f"  Warning: {len(shared)} other local project(s) share the name "
            f"'{project_name}'. They are separate stores - run `nauro projects` "
            "to inspect.\n",
            err=True,
        )


def _render_sync_status(project_id: str) -> bool:
    from nauro.cli.commands.auth import load_access_token
    from nauro.store.registry import is_cloud_project

    has_token = bool(load_access_token())
    is_cloud = is_cloud_project(project_id)
    if has_token and is_cloud:
        typer.echo("  Sync          active (event-driven, presign)")
        return True
    if not is_cloud:
        typer.echo(
            "  Sync          inactive - local-only project."
            " Enable with 'nauro auth login', then 'nauro link --cloud'."
        )
    else:
        typer.echo("  Sync          inactive - run 'nauro auth login' to enable")
    return False


def _repo_paths(project_id: str) -> list[Path]:
    try:
        from nauro.store.registry import get_repo_paths

        return [Path(path) for path in get_repo_paths(project_id)]
    except Exception:
        return []


def _render_decision_status(store_path: Path, project_id: str, *, sync_enabled: bool) -> None:
    from nauro.store.reader import _list_decisions

    local_count = len(_list_decisions(store_path))
    if not sync_enabled:
        typer.echo(f"\n  Decisions: {local_count} local")
        return

    remote_count = _count_remote_decisions(project_id)
    if remote_count is None:
        typer.echo(f"\n  Decisions: {local_count} local (could not reach remote)")
        return

    sync_label = "in sync" if local_count == remote_count else "out of sync"
    typer.echo(f"\n  Decisions: {local_count} local, {remote_count} remote ({sync_label})")

    from nauro.sync.state import load_state

    sync_state = load_state(store_path)
    if sync_state.last_full_sync:
        time_ago = _format_time_ago(sync_state.last_full_sync)
        timestamp = sync_state.last_full_sync[:19].replace("T", " ") + " UTC"
        typer.echo(f"  Last sync: {timestamp} ({time_ago})")

    if local_count != remote_count:
        typer.echo("  Run `nauro sync` to reconcile.")


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
    typer.echo(f"Store:   {store_path}\n")

    project_id = store_path.name
    _warn_if_project_name_shared(project_name, project_id)
    sync_enabled = _render_sync_status(project_id)
    _render_wiring_status(_repo_paths(project_id), no_probe=no_probe)
    _render_decision_status(store_path, project_id, sync_enabled=sync_enabled)
