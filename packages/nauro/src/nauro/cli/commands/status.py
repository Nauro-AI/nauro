"""nauro status — Show capability table for the current project."""

import functools
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar

import typer
from pydantic import BaseModel

from nauro.cli import nauro_command
from nauro.cli._codex_hooks import (
    _CODEX_HOOK_PROBE_ARGS,
    _CodexHookConfigError,
    _CodexHookState,
    _inspect_codex_hooks,
    _parse_codex_hooks,
)
from nauro.cli.integrations import codex_config, json_mcp
from nauro.cli.utils import (
    RESOLUTION_NO_PROJECT,
    DisconnectedProjectExit,
    ProjectResolutionExit,
    resolve_target_project,
)
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.local_files import UnreadableFileError, is_regular_file, read_text_or_absent


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
    """Count remote manifest entries whose path starts with ``decisions/`` and
    ends with ``.md``; ``None`` on any fetch failure. Callers must gate on auth
    and cloud-mode first; this function does not re-check.
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
    Sequential, since N repos usually share one recorded path. ``probe_nauro_command`` soft-fails,
    so a dead command costs its timeout and never raises.
    """
    return {cmd: nauro_command.probe_nauro_command(cmd, args=args) for cmd in commands}


def _repo_has_generated_agents_md(repo: Path) -> bool:
    """True when the repo's AGENTS.md carries the Nauro generation footer.
    A missing file or one without the footer is not generated; an unreadable one raises.
    """
    from nauro.templates.agents_md import FOOTER_MARKER

    text = read_text_or_absent(repo / "AGENTS.md", errors="replace")
    return text is not None and FOOTER_MARKER in text


_NO_CODEX_HOOKS = _CodexHookState(False, False, ())


def _repo_codex_hook_state(repo: Path) -> _CodexHookState:
    """Return presence, structural completeness, and commands for Codex hooks.
    A missing or off-shape file is not wired; an unreadable or unparseable one raises.
    """
    path = repo / ".codex" / "hooks.json"
    text = read_text_or_absent(path)
    if text is None:
        return _NO_CODEX_HOOKS
    try:
        config = _parse_codex_hooks(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise UnreadableFileError(path, f"invalid JSON: {exc}") from exc
    except _CodexHookConfigError:
        return _NO_CODEX_HOOKS
    return _inspect_codex_hooks(config, windows=_is_windows())


T = TypeVar("T")


@dataclass(frozen=True)
class _Probe(Generic[T]):
    """One on-disk observation: what could be read, and the files that could not."""

    value: T
    unreadable: tuple[UnreadableFileError, ...] = ()


def _probe(read: Callable[[], T], fallback: T) -> _Probe[T]:
    """Run one read; an unreadable file yields ``fallback`` with the failure recorded."""
    try:
        return _Probe(read())
    except UnreadableFileError as exc:
        return _Probe(fallback, (exc,))


def _probe_each(
    items: Iterable[Path], read: Callable[[Path], T], fallback: T
) -> _Probe[tuple[T, ...]]:
    """Run ``read`` per item, keeping every readable value and every failure."""
    probes = [_probe(functools.partial(read, item), fallback) for item in items]
    return _Probe(
        tuple(probe.value for probe in probes),
        tuple(failure for probe in probes for failure in probe.unreadable),
    )


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
class _WorkflowAgentCounts:
    """Workflow agents tallied on Claude Code, Cursor, and Codex."""

    claude: _ArtifactCounts
    cursor: _ArtifactCounts
    codex: _ArtifactCounts

    @property
    def present(self) -> int:
        return self.claude.present + self.cursor.present + self.codex.present

    @property
    def current(self) -> int:
        return self.claude.current + self.cursor.current + self.codex.current

    @property
    def expected(self) -> int:
        return self.claude.expected + self.cursor.expected + self.codex.expected

    @property
    def stale(self) -> int:
        return self.present - self.current

    @property
    def fully_current(self) -> bool:
        return self.current == self.expected


_NO_AGENT_COUNTS = _WorkflowAgentCounts(
    claude=_NO_COUNTS,
    cursor=_NO_COUNTS,
    codex=_NO_COUNTS,
)


@dataclass(frozen=True)
class _SkillArtifacts:
    """Nauro-owned skills across surfaces.

    Core skills install on every adopt/setup-all run; opt-in skills install
    only behind their flag, so their absence is a chosen state, not a wiring
    defect. Legacy copies under ``~/.codex/skills`` are counted for migration.
    """

    core_skills: _SurfacePair
    opt_in_skills: _SurfacePair
    legacy_codex_skills: int


_NO_SKILL_ARTIFACTS = _SkillArtifacts(
    core_skills=_NO_PAIR, opt_in_skills=_NO_PAIR, legacy_codex_skills=0
)


@dataclass(frozen=True)
class _RegistryFacts:
    """What the registry says about one project: its repos and whether it is cloud-mode."""

    repos: tuple[Path, ...] = ()
    cloud: bool = False


@dataclass(frozen=True)
class _CodexGlobal:
    wired: bool = False
    command: str | None = None


@dataclass(frozen=True)
class _WiringSnapshot:
    """Every wiring fact as a probe: the observed value plus the files that could not be read."""

    registry: _RegistryFacts
    repo_mcp: _Probe[tuple[tuple[str | None, ...], ...]]
    codex: _Probe[_CodexGlobal]
    hooks: _Probe[tuple[_CodexHookState, ...]]
    agents: _Probe[int]
    skills: _Probe[_SkillArtifacts]
    workflow_agents: _Probe[_WorkflowAgentCounts]
    untrusted_commands: frozenset[str] = frozenset()

    @property
    def repo_count(self) -> int:
        return len(self.registry.repos)

    @property
    def mcp_wired(self) -> int:
        return sum(1 for commands in self.repo_mcp.value if commands)

    @property
    def codex_global(self) -> bool:
        return self.codex.value.wired

    @property
    def mcp_commands(self) -> frozenset[str]:
        recorded = {command for commands in self.repo_mcp.value for command in commands if command}
        if self.codex.value.command:
            recorded.add(self.codex.value.command)
        return frozenset(recorded)

    @property
    def hook_states(self) -> tuple[_CodexHookState, ...]:
        return self.hooks.value

    @property
    def agents_generated(self) -> int:
        return self.agents.value

    def _unreadable(self, *probes: _Probe) -> tuple[UnreadableFileError, ...]:
        return tuple(failure for probe in probes for failure in probe.unreadable)

    @property
    def mcp_unreadable(self) -> tuple[UnreadableFileError, ...]:
        return self._unreadable(self.repo_mcp, self.codex)

    @property
    def hooks_unreadable(self) -> tuple[UnreadableFileError, ...]:
        return self._unreadable(self.hooks)

    @property
    def agents_unreadable(self) -> tuple[UnreadableFileError, ...]:
        return self._unreadable(self.agents)

    @property
    def skills_unreadable(self) -> tuple[UnreadableFileError, ...]:
        return self._unreadable(self.skills)

    @property
    def workflow_unreadable(self) -> tuple[UnreadableFileError, ...]:
        return self._unreadable(self.workflow_agents)

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

    @property
    def untrusted_mcp_commands(self) -> frozenset[str]:
        return self.mcp_commands & self.untrusted_commands

    @property
    def untrusted_hook_commands(self) -> frozenset[str]:
        return self.hook_commands & self.untrusted_commands


@dataclass(frozen=True)
class _WiringProbeResults:
    skipped: bool
    mcp: dict[str, bool] | None
    hooks: dict[str, bool] | None


def _count_artifacts(expected: dict[Path, str]) -> _Probe[_ArtifactCounts]:
    """Tally how many bundled artifacts are present and byte-current; unreadable ones are
    recorded and the rest still counted."""
    present = 0
    current = 0
    unreadable: list[UnreadableFileError] = []
    for path, bundled in expected.items():
        try:
            content = read_text_or_absent(path)
        except UnreadableFileError as exc:
            unreadable.append(exc)
            continue
        if content is None:
            continue
        present += 1
        if content == bundled:
            current += 1
    counts = _ArtifactCounts(expected=len(expected), present=present, current=current)
    return _Probe(counts, tuple(unreadable))


def _failures(*probes: _Probe) -> tuple[UnreadableFileError, ...]:
    return tuple(failure for probe in probes for failure in probe.unreadable)


def _count_skills(surface: str, base: Path, names: tuple[str, ...]) -> _Probe[_ArtifactCounts]:
    from nauro.skills import render_skill

    return _count_artifacts(
        {base / name / "SKILL.md": render_skill(surface, name) for name in names}
    )


def _skill_artifacts() -> _Probe[_SkillArtifacts]:
    """Inspect Nauro-owned skills on the Claude Code and Codex surfaces."""
    from nauro.cli.integrations.skills import OPT_IN_SKILL_NAMES, SKILL_NAMES

    claude_base = Path.home() / ".claude" / "skills"
    codex_base = Path.home() / ".agents" / "skills"
    core_claude = _count_skills("claude_code", claude_base, SKILL_NAMES)
    core_codex = _count_skills("codex", codex_base, SKILL_NAMES)
    opt_in_claude = _count_skills("claude_code", claude_base, OPT_IN_SKILL_NAMES)
    opt_in_codex = _count_skills("codex", codex_base, OPT_IN_SKILL_NAMES)
    legacy_paths = [
        Path.home() / ".codex" / "skills" / name / "SKILL.md"
        for name in SKILL_NAMES + OPT_IN_SKILL_NAMES
    ]
    legacy = _probe_each(legacy_paths, is_regular_file, False)
    artifacts = _SkillArtifacts(
        core_skills=_SurfacePair(claude=core_claude.value, codex=core_codex.value),
        opt_in_skills=_SurfacePair(claude=opt_in_claude.value, codex=opt_in_codex.value),
        legacy_codex_skills=sum(legacy.value),
    )
    return _Probe(
        artifacts, _failures(core_claude, core_codex, opt_in_claude, opt_in_codex, legacy)
    )


def _agent_artifacts(repo_paths: list[Path]) -> _Probe[_WorkflowAgentCounts]:
    """Inspect Nauro's workflow agents on Claude Code, Cursor (per repo) and Codex."""
    from nauro.agents import AGENT_NAMES, render_agent

    claude = _count_artifacts(
        {
            Path.home() / ".claude" / "agents" / f"{name}.md": render_agent("claude_code", name)
            for name in AGENT_NAMES
        }
    )
    cursor = _count_artifacts(
        {
            repo / ".cursor" / "agents" / f"{name}.md": render_agent("cursor", name)
            for repo in repo_paths
            for name in AGENT_NAMES
        }
    )
    codex = _count_artifacts(
        {
            Path.home() / ".codex" / "agents" / f"{name}.toml": render_agent("codex", name)
            for name in AGENT_NAMES
        }
    )
    counts = _WorkflowAgentCounts(claude=claude.value, cursor=cursor.value, codex=codex.value)
    return _Probe(counts, _failures(claude, cursor, codex))


def _collect_wiring(registry: _RegistryFacts) -> _WiringSnapshot:
    repos = registry.repos
    generated = _probe_each(repos, _repo_has_generated_agents_md, False)
    wirings = [json_mcp.recorded_mcp_commands(repo) for repo in repos]
    snapshot = _WiringSnapshot(
        registry=registry,
        repo_mcp=_Probe(
            tuple(wiring.commands for wiring in wirings),
            tuple(failure for wiring in wirings for failure in wiring.unreadable),
        ),
        codex=_probe(lambda: _CodexGlobal(*codex_config.recorded_codex_command()), _CodexGlobal()),
        hooks=_probe_each(repos, _repo_codex_hook_state, _NO_CODEX_HOOKS),
        agents=_Probe(sum(generated.value), generated.unreadable),
        skills=_skill_artifacts(),
        workflow_agents=_agent_artifacts(list(repos)),
    )
    recorded = snapshot.mcp_commands | snapshot.hook_commands
    return replace(snapshot, untrusted_commands=_untrusted_commands(recorded, list(repos)))


def _untrusted_commands(commands: frozenset[str], repo_paths: list[Path]) -> frozenset[str]:
    """Recorded commands status must never execute."""
    return frozenset(
        command for command in commands if not nauro_command.is_probe_safe(command, repo_paths)
    )


def _probe_wiring(snapshot: _WiringSnapshot, *, no_probe: bool) -> _WiringProbeResults:
    if no_probe:
        return _WiringProbeResults(True, None, None)
    untrusted = snapshot.untrusted_commands
    mcp_results = _probe_commands(snapshot.mcp_commands - untrusted, args=("--version",))
    hook_results = _probe_commands(snapshot.hook_commands - untrusted, args=_CODEX_HOOK_PROBE_ARGS)
    return _WiringProbeResults(False, mcp_results, hook_results)


def _probe_commands(
    commands: frozenset[str],
    *,
    args: tuple[str, ...],
) -> dict[str, bool] | None:
    if not commands:
        return None
    return _probe_distinct_commands(set(commands), args=args)


def _unreadable_clause(failures: tuple[UnreadableFileError, ...]) -> str:
    first = failures[0]
    more = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
    return f"could not read {first.path}: {first.reason}{more}"


def _unknown_line(row: str, failures: tuple[UnreadableFileError, ...]) -> str:
    """The third row state: the fact could not be observed because a file could not be read."""
    return f"  {row:<14}unknown - {_unreadable_clause(failures)}"


def _broken_line(line: str, failures: tuple[UnreadableFileError, ...]) -> str:
    """A defect observed in a readable file stays BROKEN; an unreadable sibling is appended."""
    return f"{line}; {_unreadable_clause(failures)}" if failures else line


def _mcp_status_line(snapshot: _WiringSnapshot, probes: _WiringProbeResults) -> str:
    failures = snapshot.mcp_unreadable
    if not snapshot.mcp_wired and not snapshot.codex_global:
        if failures:
            return _unknown_line("MCP", failures)
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
    if not healthy:
        return _broken_line(
            f"  MCP           BROKEN - {detail} but the recorded command won't run; "
            "re-run 'nauro setup all'",
            failures,
        )
    if failures:
        return _unknown_line("MCP", failures)
    if snapshot.untrusted_mcp_commands:
        untrusted = _untrusted_detail(snapshot.untrusted_mcp_commands)
        return f"  MCP           active ({detail}; {untrusted})"
    return f"  MCP           active ({detail})"


def _untrusted_detail(commands: frozenset[str]) -> str:
    listed = ", ".join(repr(command) for command in sorted(commands))
    return f"{listed} is not a nauro install, not probed"


def _codex_hooks_status_line(snapshot: _WiringSnapshot, probes: _WiringProbeResults) -> str:
    failures = snapshot.hooks_unreadable
    configured = snapshot.configured_hooks
    if not configured:
        if failures:
            return _unknown_line("Codex hooks", failures)
        return "  Codex hooks   inactive - run 'nauro setup codex --with-hooks'"
    detail = f"wired in {len(configured)}/{snapshot.repo_count} repos"
    complete = all(
        state.complete and state.recorded_commands and all(state.recorded_commands)
        for state in configured
    )
    if not complete:
        return _broken_line(
            f"  Codex hooks   BROKEN - {detail} but the lifecycle wiring is incomplete; "
            "re-run 'nauro setup all --with-hooks'",
            failures,
        )
    healthy = probes.hooks is None or all(
        probes.hooks.get(command, True) for command in snapshot.hook_commands
    )
    if not healthy:
        return _broken_line(
            f"  Codex hooks   BROKEN - {detail} but the recorded command won't run; "
            "re-run 'nauro setup all --with-hooks'",
            failures,
        )
    if failures:
        return _unknown_line("Codex hooks", failures)
    if snapshot.untrusted_hook_commands:
        untrusted = _untrusted_detail(snapshot.untrusted_hook_commands)
        return f"  Codex hooks   configured ({detail}; {untrusted})"
    if probes.skipped:
        return f"  Codex hooks   configured ({detail}; liveness not probed)"
    return f"  Codex hooks   configured ({detail}; command healthy)"


def _agents_status_line(snapshot: _WiringSnapshot) -> str:
    if snapshot.agents_unreadable:
        return _unknown_line("AGENTS.md", snapshot.agents_unreadable)
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


def _workflow_agent_detail(counts: _WorkflowAgentCounts) -> str:
    """Per-surface current and expected workflow-agent counts."""
    return (
        f"Claude {counts.claude.current}/{counts.claude.expected}; "
        f"Cursor {counts.cursor.current}/{counts.cursor.expected}; "
        f"Codex {counts.codex.current}/{counts.codex.expected}"
    )


_SKILLS_INACTIVE_LINE = (
    "  Skills        inactive - run 'nauro setup all' (--with-skills adds the opt-in skills)"
)


def _skills_status_line(snapshot: _WiringSnapshot) -> str:
    """Render the Skills row.
    Missing core skills are a wiring defect; opt-in skills absent in full stay inside an "active"
    row. Stale files and legacy ``~/.codex/skills`` copies are BROKEN.
    """
    failures = snapshot.skills_unreadable
    skills = snapshot.skills.value
    core, opt_in = skills.core_skills, skills.opt_in_skills
    if skills.legacy_codex_skills:
        count = skills.legacy_codex_skills
        plural = "copy" if count == 1 else "copies"
        return _broken_line(
            f"  Skills        BROKEN - {count} legacy Nauro skill {plural} under "
            "~/.codex/skills; migrate with 'nauro setup all --with-skills' or remove manually",
            failures,
        )
    if core.stale or opt_in.stale:
        remedy = "nauro setup all --with-skills" if opt_in.stale else "nauro setup all"
        return _broken_line(
            f"  Skills        BROKEN - {_surface_detail(core, opt_in)}; installed Nauro "
            f"skill files differ from this release; run '{remedy}'",
            failures,
        )
    if failures:
        return _unknown_line("Skills", failures)
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
    failures = snapshot.workflow_unreadable
    agents = snapshot.workflow_agents.value
    detail = _workflow_agent_detail(agents)
    if agents.stale:
        return _broken_line(
            f"  Workflow      BROKEN - {detail}; installed Nauro agent files differ from "
            "this release; run 'nauro setup all --with-subagents'",
            failures,
        )
    if failures:
        return _unknown_line("Workflow", failures)
    if agents.present == 0:
        return (
            "  Workflow      not installed (opt-in) - "
            "'nauro setup all --with-subagents' adds the workflow agents"
        )
    if not agents.fully_current:
        return f"  Workflow      partial ({detail}) - run 'nauro setup all --with-subagents'"
    return f"  Workflow      active ({detail})"


def _count_shared_names(project_name: str, project_id: str) -> int:
    try:
        from nauro.store.registry import find_projects_by_name_v2

        return sum(
            1
            for candidate_id, _ in find_projects_by_name_v2(project_name)
            if candidate_id != project_id
        )
    except Exception:
        return 0


def _registry_facts(project_id: str) -> _RegistryFacts:
    from nauro.store.registry import get_project_entry_v2

    entry = get_project_entry_v2(project_id)
    if entry is None:
        return _RegistryFacts()
    repos = tuple(Path(path) for path in entry.repo_paths)
    return _RegistryFacts(repos, entry.mode == REPO_CONFIG_MODE_CLOUD)


@dataclass(frozen=True)
class _QuarantineReport:
    """Quarantined collisions, or the fact that they could not be listed."""

    collisions: tuple[str, ...] = ()
    readable: bool = True


@dataclass(frozen=True)
class _StatusFacts:
    """Everything one status run observed, gathered before any output."""

    project_name: str
    store_path: Path
    shared_name_count: int
    authenticated: bool
    cloud: bool
    snapshot: _WiringSnapshot
    probes: _WiringProbeResults
    local_decisions: int
    remote_decisions: int | None
    last_full_sync: str | None
    quarantine: _QuarantineReport

    @property
    def project_id(self) -> str:
        return self.store_path.name

    @property
    def sync_enabled(self) -> bool:
        return self.authenticated and self.cloud


def _collect_status(project_name: str, store_path: Path, *, no_probe: bool) -> _StatusFacts:
    """Gather every fact the human table and the JSON payload render from."""
    from nauro.auth import load_access_token
    from nauro.store.reader import _list_decisions

    project_id = store_path.name
    authenticated = bool(load_access_token())
    registry = _registry_facts(project_id)
    cloud = registry.cloud
    sync_enabled = authenticated and cloud
    snapshot = _collect_wiring(registry)
    probes = _probe_wiring(snapshot, no_probe=no_probe)

    remote_decisions = _count_remote_decisions(project_id) if sync_enabled else None
    last_full_sync: str | None = None
    if sync_enabled:
        from nauro.sync.state import load_state

        # load_state only guards JSON decode errors: valid JSON of the wrong
        # shape raises AttributeError, an unreadable file PermissionError,
        # non-UTF-8 bytes UnicodeDecodeError. And a parseable state object
        # passes any last_full_sync value through untyped, so a non-string
        # would later crash both output modes. A broken sync-state file must
        # degrade to "no recorded sync", never crash status.
        try:
            recorded = load_state(store_path).last_full_sync
        except Exception:
            recorded = None
        last_full_sync = recorded if isinstance(recorded, str) and recorded else None

    return _StatusFacts(
        project_name=project_name,
        store_path=store_path,
        shared_name_count=_count_shared_names(project_name, project_id),
        authenticated=authenticated,
        cloud=cloud,
        snapshot=snapshot,
        probes=probes,
        local_decisions=len(_list_decisions(store_path)),
        remote_decisions=remote_decisions,
        last_full_sync=last_full_sync,
        quarantine=_quarantined_collisions(store_path),
    )


def _quarantined_collisions(store_path: Path) -> _QuarantineReport:
    """Return the remote decisions a pull quarantined and no later pull installed.
    A quarantine records no sync state, so this line is what keeps an unresolved collision
    visible. A failure to list them is reported as a failure, never as "none".
    """
    try:
        from nauro.sync.quarantine import unresolved_quarantines

        return _QuarantineReport(
            collisions=tuple(item.label for item in unresolved_quarantines(store_path))
        )
    except Exception:
        return _QuarantineReport(readable=False)


# ── Human table emission ────────────────────────────────────────────────────


def _emit_human_status(facts: _StatusFacts) -> None:
    typer.echo(f"Project: {facts.project_name}")
    typer.echo(f"Store:   {facts.store_path}\n")

    if facts.shared_name_count:
        typer.echo(
            f"  Warning: {facts.shared_name_count} other local project(s) share the name "
            f"'{facts.project_name}'. They are separate stores - run `nauro projects` "
            "to inspect.\n",
            err=True,
        )
    _emit_sync_status(facts)
    typer.echo(_mcp_status_line(facts.snapshot, facts.probes))
    typer.echo(_codex_hooks_status_line(facts.snapshot, facts.probes))
    typer.echo(_skills_status_line(facts.snapshot))
    typer.echo(_workflow_agents_status_line(facts.snapshot))
    typer.echo(_agents_status_line(facts.snapshot))
    _emit_decision_status(facts)
    _emit_quarantine_status(facts)


def _emit_sync_status(facts: _StatusFacts) -> None:
    if facts.sync_enabled:
        typer.echo("  Sync          active (event-driven, presign)")
    elif not facts.cloud:
        typer.echo(
            "  Sync          inactive - local-only project."
            " Enable with 'nauro auth login', then 'nauro link --cloud'."
        )
    else:
        typer.echo("  Sync          inactive - run 'nauro auth login' to enable")


def _emit_decision_status(facts: _StatusFacts) -> None:
    local_count = facts.local_decisions
    if not facts.sync_enabled:
        typer.echo(f"\n  Decisions: {local_count} local")
        return

    remote_count = facts.remote_decisions
    if remote_count is None:
        typer.echo(f"\n  Decisions: {local_count} local (could not reach remote)")
        return

    sync_label = "in sync" if local_count == remote_count else "out of sync"
    typer.echo(f"\n  Decisions: {local_count} local, {remote_count} remote ({sync_label})")

    if facts.last_full_sync:
        time_ago = _format_time_ago(facts.last_full_sync)
        timestamp = facts.last_full_sync[:19].replace("T", " ") + " UTC"
        typer.echo(f"  Last sync: {timestamp} ({time_ago})")

    if local_count != remote_count:
        typer.echo("  Run `nauro sync` to reconcile.")


def _emit_quarantine_status(facts: _StatusFacts) -> None:
    if not facts.quarantine.readable:
        typer.echo(
            "\n  Quarantined decision-number collisions: could not be read"
            " - run `nauro sync` for the full report."
        )
        return
    quarantined = facts.quarantine.collisions
    if not quarantined:
        return
    typer.echo(f"\n  Quarantined decision-number collisions: {len(quarantined)}")
    for remote_path in quarantined:
        typer.echo(f"    - {remote_path} was not installed; a local file holds that number")
    typer.echo("  Run `nauro sync` for the full report on each one.")


# ── JSON payload ────────────────────────────────────────────────────────────


class _CountsPayload(BaseModel):
    present: int
    current: int
    expected: int


class _SurfaceCountsPayload(BaseModel):
    claude: _CountsPayload
    codex: _CountsPayload


class _WorkflowAgentCountsPayload(BaseModel):
    claude: _CountsPayload
    cursor: _CountsPayload
    codex: _CountsPayload
    unreadable: list[str]


class _SyncPayload(BaseModel):
    cloud: bool
    authenticated: bool
    active: bool


class _McpPayload(BaseModel):
    repo_count: int
    wired_repos: int
    codex_global: bool
    probed: bool
    healthy: bool | None
    untrusted_commands: int
    unreadable: list[str]


class _CodexHooksPayload(BaseModel):
    repo_count: int
    configured_repos: int
    complete: bool | None
    probed: bool
    healthy: bool | None
    untrusted_commands: int
    unreadable: list[str]


class _SkillsPayload(BaseModel):
    core: _SurfaceCountsPayload
    opt_in: _SurfaceCountsPayload
    legacy_codex_copies: int
    unreadable: list[str]


class _AgentsMdPayload(BaseModel):
    repo_count: int
    generated_repos: int
    unreadable: list[str]


class _DecisionsPayload(BaseModel):
    local: int
    remote: int | None
    in_sync: bool | None
    last_full_sync: str | None
    quarantined_collisions: list[str] | None


class StatusPayload(BaseModel):
    """Curated machine-readable projection of one status run.

    Internal wiring state (recorded command strings, per-repo hook tuples)
    stays out deliberately: the payload carries capability facts, not the
    probe plumbing.
    """

    project: str
    project_id: str
    store_path: str
    shared_name_count: int
    sync: _SyncPayload
    mcp: _McpPayload
    codex_hooks: _CodexHooksPayload
    skills: _SkillsPayload
    workflow_agents: _WorkflowAgentCountsPayload
    agents_md: _AgentsMdPayload
    decisions: _DecisionsPayload


def _counts_payload(counts: _ArtifactCounts) -> _CountsPayload:
    return _CountsPayload(present=counts.present, current=counts.current, expected=counts.expected)


def _surface_counts_payload(pair: _SurfacePair) -> _SurfaceCountsPayload:
    return _SurfaceCountsPayload(
        claude=_counts_payload(pair.claude), codex=_counts_payload(pair.codex)
    )


def _workflow_agent_counts_payload(
    counts: _WorkflowAgentCounts, unreadable: tuple[UnreadableFileError, ...]
) -> _WorkflowAgentCountsPayload:
    return _WorkflowAgentCountsPayload(
        claude=_counts_payload(counts.claude),
        cursor=_counts_payload(counts.cursor),
        codex=_counts_payload(counts.codex),
        unreadable=_paths(unreadable),
    )


def _paths(failures: tuple[UnreadableFileError, ...]) -> list[str]:
    return [str(failure.path) for failure in failures]


def _build_status_payload(facts: _StatusFacts) -> StatusPayload:
    snapshot, probes = facts.snapshot, facts.probes

    mcp_probed = not probes.skipped and probes.mcp is not None
    mcp_healthy = (
        all(probes.mcp.get(command, True) for command in snapshot.mcp_commands)
        if mcp_probed
        else None
    )

    configured = snapshot.configured_hooks
    # Null when nothing is configured — completeness is not applicable, same
    # idiom as the healthy fields when nothing was probed.
    hooks_complete = (
        all(
            state.complete and state.recorded_commands and all(state.recorded_commands)
            for state in configured
        )
        if configured
        else None
    )
    hooks_probed = not probes.skipped and probes.hooks is not None
    hooks_healthy = (
        all(probes.hooks.get(command, True) for command in snapshot.hook_commands)
        if hooks_probed
        else None
    )

    skills = snapshot.skills.value
    return StatusPayload(
        project=facts.project_name,
        project_id=facts.project_id,
        store_path=str(facts.store_path),
        shared_name_count=facts.shared_name_count,
        sync=_SyncPayload(
            cloud=facts.cloud,
            authenticated=facts.authenticated,
            active=facts.sync_enabled,
        ),
        mcp=_McpPayload(
            repo_count=snapshot.repo_count,
            wired_repos=snapshot.mcp_wired,
            codex_global=snapshot.codex_global,
            probed=mcp_probed,
            healthy=mcp_healthy,
            untrusted_commands=len(snapshot.untrusted_mcp_commands),
            unreadable=_paths(snapshot.mcp_unreadable),
        ),
        codex_hooks=_CodexHooksPayload(
            repo_count=snapshot.repo_count,
            configured_repos=len(configured),
            complete=hooks_complete,
            probed=hooks_probed,
            healthy=hooks_healthy,
            untrusted_commands=len(snapshot.untrusted_hook_commands),
            unreadable=_paths(snapshot.hooks_unreadable),
        ),
        skills=_SkillsPayload(
            core=_surface_counts_payload(skills.core_skills),
            opt_in=_surface_counts_payload(skills.opt_in_skills),
            legacy_codex_copies=skills.legacy_codex_skills,
            unreadable=_paths(snapshot.skills_unreadable),
        ),
        workflow_agents=_workflow_agent_counts_payload(
            snapshot.workflow_agents.value, snapshot.workflow_unreadable
        ),
        agents_md=_AgentsMdPayload(
            repo_count=snapshot.repo_count,
            generated_repos=snapshot.agents_generated,
            unreadable=_paths(snapshot.agents_unreadable),
        ),
        decisions=_DecisionsPayload(
            local=facts.local_decisions,
            remote=facts.remote_decisions,
            in_sync=(
                None
                if facts.remote_decisions is None
                else facts.local_decisions == facts.remote_decisions
            ),
            last_full_sync=facts.last_full_sync,
            quarantined_collisions=(
                list(facts.quarantine.collisions) if facts.quarantine.readable else None
            ),
        ),
    )


_STATUS_NO_PROJECT_MESSAGE = "No project found. Run 'nauro init <name>' to get started."


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
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the capability report as machine-readable JSON.",
    ),
) -> None:
    """Show which Nauro capabilities are active or inactive."""
    try:
        project_name, store_path = resolve_target_project(project)
    except typer.Exit as exc:
        if not isinstance(exc, DisconnectedProjectExit):
            typer.echo(_STATUS_NO_PROJECT_MESSAGE, err=True)
        if json_output:
            # A plain typer.Exit without resolution fields should not occur;
            # map it defensively to the no-project reason.
            if isinstance(exc, ProjectResolutionExit):
                reason, guidance = exc.reason, exc.guidance
            else:
                reason, guidance = RESOLUTION_NO_PROJECT, _STATUS_NO_PROJECT_MESSAGE
            envelope = {"status": "error", "error": {"reason": reason, "guidance": guidance}}
            typer.echo(json.dumps(envelope, indent=2))
        raise typer.Exit(exc.exit_code) from exc

    from nauro.cli.generation_reads import status_command

    if status_command(store_path.name, json_output=json_output):
        return
    facts = _collect_status(project_name, store_path, no_probe=no_probe)
    if json_output:
        typer.echo(json.dumps(_build_status_payload(facts).model_dump(), indent=2))
        return
    _emit_human_status(facts)
