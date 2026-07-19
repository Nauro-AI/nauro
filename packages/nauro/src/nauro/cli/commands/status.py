"""nauro status — Show capability table for the current project."""

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
from nauro.cli.integrations import codex_config, json_mcp
from nauro.cli.utils import DisconnectedProjectExit, resolve_target_project


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
class _ArtifactCounts:
    """Tally of one bundled artifact set on one surface."""

    expected: int
    present: int
    current: int


_NO_COUNTS = _ArtifactCounts(expected=0, present=0, current=0)


@dataclass(frozen=True)
class _SurfacePair:
    """The same artifact set tallied on the Claude Code and Codex surfaces."""

    claude: _ArtifactCounts
    codex: _ArtifactCounts

    @property
    def present(self) -> int:
        return self.claude.present + self.codex.present

    @property
    def current(self) -> int:
        return self.claude.current + self.codex.current

    @property
    def expected(self) -> int:
        return self.claude.expected + self.codex.expected

    @property
    def stale(self) -> int:
        return self.present - self.current

    @property
    def fully_current(self) -> bool:
        return self.current == self.expected


_NO_PAIR = _SurfacePair(claude=_NO_COUNTS, codex=_NO_COUNTS)


@dataclass(frozen=True)
class _WorkflowArtifacts:
    """Nauro-owned skills and workflow agents across both user surfaces.

    Core skills install on every adopt/setup-all run; opt-in skills and
    workflow agents install only behind their flags, so their absence is a
    chosen state, not a wiring defect.
    """

    core_skills: _SurfacePair
    opt_in_skills: _SurfacePair
    agents: _SurfacePair
    legacy_codex_skills: int


_NO_WORKFLOW_ARTIFACTS = _WorkflowArtifacts(
    core_skills=_NO_PAIR,
    opt_in_skills=_NO_PAIR,
    agents=_NO_PAIR,
    legacy_codex_skills=0,
)


@dataclass(frozen=True)
class _WiringSnapshot:
    repo_count: int
    mcp_wired: int
    codex_global: bool
    mcp_commands: frozenset[str]
    hook_states: tuple[_CodexHookState, ...]
    agents_generated: int
    workflow: _WorkflowArtifacts

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


def _count_artifacts(expected: dict[Path, str]) -> _ArtifactCounts:
    """Tally how many bundled artifacts are present and byte-current on disk."""
    present = 0
    current = 0
    for path, bundled in expected.items():
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        present += 1
        if content == bundled:
            current += 1
    return _ArtifactCounts(expected=len(expected), present=present, current=current)


def _count_skills(surface: str, base: Path, names: tuple[str, ...]) -> _ArtifactCounts:
    from nauro.skills import render_skill

    return _count_artifacts(
        {base / name / "SKILL.md": render_skill(surface, name) for name in names}
    )


def _workflow_artifacts() -> _WorkflowArtifacts:
    """Inspect Nauro-owned skills and workflow agents on both user surfaces."""
    from nauro.agents import AGENT_NAMES, render_agent
    from nauro.cli.integrations.skills import OPT_IN_SKILL_NAMES, SKILL_NAMES

    claude_skill_base = Path.home() / ".claude" / "skills"
    codex_skill_base = Path.home() / ".agents" / "skills"
    claude_agent_base = Path.home() / ".claude" / "agents"
    codex_agent_base = Path.home() / ".codex" / "agents"

    agents = _SurfacePair(
        claude=_count_artifacts(
            {
                claude_agent_base / f"{name}.md": render_agent("claude_code", name)
                for name in AGENT_NAMES
            }
        ),
        codex=_count_artifacts(
            {codex_agent_base / f"{name}.toml": render_agent("codex", name) for name in AGENT_NAMES}
        ),
    )
    legacy_codex_skills = sum(
        1
        for name in SKILL_NAMES + OPT_IN_SKILL_NAMES
        if (Path.home() / ".codex" / "skills" / name / "SKILL.md").is_file()
    )
    return _WorkflowArtifacts(
        core_skills=_SurfacePair(
            claude=_count_skills("claude_code", claude_skill_base, SKILL_NAMES),
            codex=_count_skills("codex", codex_skill_base, SKILL_NAMES),
        ),
        opt_in_skills=_SurfacePair(
            claude=_count_skills("claude_code", claude_skill_base, OPT_IN_SKILL_NAMES),
            codex=_count_skills("codex", codex_skill_base, OPT_IN_SKILL_NAMES),
        ),
        agents=agents,
        legacy_codex_skills=legacy_codex_skills,
    )


def _collect_wiring(repo_paths: list[Path]) -> _WiringSnapshot:
    try:
        repo_commands = [json_mcp.recorded_mcp_commands(repo) for repo in repo_paths]
    except Exception:
        repo_commands = []
    try:
        codex_global, codex_command = codex_config.recorded_codex_command()
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
    try:
        workflow = _workflow_artifacts()
    except Exception:
        workflow = _NO_WORKFLOW_ARTIFACTS

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
        workflow=workflow,
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


def _surface_detail(*pairs: _SurfacePair) -> str:
    """Per-surface current/expected summary across one or more artifact sets."""
    claude_current = sum(pair.claude.current for pair in pairs)
    claude_expected = sum(pair.claude.expected for pair in pairs)
    codex_current = sum(pair.codex.current for pair in pairs)
    codex_expected = sum(pair.codex.expected for pair in pairs)
    return f"Claude {claude_current}/{claude_expected}; Codex {codex_current}/{codex_expected}"


_SKILLS_INACTIVE_LINE = (
    "  Skills        inactive - run 'nauro setup all' (--with-skills adds the opt-in skills)"
)


def _skills_status_line(snapshot: _WiringSnapshot) -> str:
    """Render the Skills row.

    Core skills install unconditionally, so a gap there is a wiring defect;
    opt-in skills absent in full is a chosen state and stays inside an
    "active" row. Stale files and legacy ~/.codex/skills copies are BROKEN.
    """
    workflow = snapshot.workflow
    core, opt_in = workflow.core_skills, workflow.opt_in_skills
    if workflow.legacy_codex_skills:
        count = workflow.legacy_codex_skills
        plural = "copy" if count == 1 else "copies"
        return (
            f"  Skills        BROKEN - {count} legacy Nauro skill {plural} under "
            "~/.codex/skills; migrate with 'nauro setup all --with-skills' or remove manually"
        )
    if core.stale or opt_in.stale:
        remedy = "nauro setup all --with-skills" if opt_in.stale else "nauro setup all"
        return (
            f"  Skills        BROKEN - {_surface_detail(core, opt_in)}; installed Nauro "
            f"skill files differ from this release; run '{remedy}'"
        )
    if core.expected == 0:
        return _SKILLS_INACTIVE_LINE
    if not core.fully_current:
        if core.present == 0 and opt_in.present == 0:
            return _SKILLS_INACTIVE_LINE
        return f"  Skills        partial ({_surface_detail(core, opt_in)}) - run 'nauro setup all'"
    if opt_in.present == 0:
        return (
            "  Skills        active (core installed; opt-in skills not installed - "
            "'nauro setup all --with-skills' adds them)"
        )
    if not opt_in.fully_current:
        return (
            f"  Skills        partial ({_surface_detail(core, opt_in)}) - "
            "run 'nauro setup all --with-skills'"
        )
    return f"  Skills        active ({_surface_detail(core, opt_in)})"


def _workflow_agents_status_line(snapshot: _WiringSnapshot) -> str:
    """Render the Workflow row. The agents are opt-in, so full absence is a
    stated choice; a partial or stale install is a defect."""
    agents = snapshot.workflow.agents
    detail = _surface_detail(agents)
    if agents.stale:
        return (
            f"  Workflow      BROKEN - {detail}; installed Nauro agent files differ from "
            "this release; run 'nauro setup all --with-subagents'"
        )
    if agents.present == 0:
        return (
            "  Workflow      not installed (opt-in) - "
            "'nauro setup all --with-subagents' adds the workflow agents"
        )
    if not agents.fully_current:
        return f"  Workflow      partial ({detail}) - run 'nauro setup all --with-subagents'"
    return f"  Workflow      active ({detail})"


def _render_wiring_status(repo_paths: list[Path], *, no_probe: bool) -> None:
    snapshot = _collect_wiring(repo_paths)
    probes = _probe_wiring(snapshot, no_probe=no_probe)
    typer.echo(_mcp_status_line(snapshot, probes))
    typer.echo(_codex_hooks_status_line(snapshot, probes))
    typer.echo(_skills_status_line(snapshot))
    typer.echo(_workflow_agents_status_line(snapshot))
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
        if not isinstance(exc, DisconnectedProjectExit):
            typer.echo("No project found. Run 'nauro init <name>' to get started.", err=True)
        raise typer.Exit(exc.exit_code) from exc

    typer.echo(f"Project: {project_name}")
    typer.echo(f"Store:   {store_path}\n")

    project_id = store_path.name
    _warn_if_project_name_shared(project_name, project_id)
    sync_enabled = _render_sync_status(project_id)
    _render_wiring_status(_repo_paths(project_id), no_probe=no_probe)
    _render_decision_status(store_path, project_id, sync_enabled=sync_enabled)
