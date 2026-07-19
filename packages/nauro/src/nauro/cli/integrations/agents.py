"""Agent (subagent) artifact codec for the setup surface."""

from __future__ import annotations

from pathlib import Path

from nauro.cli.integrations.outcomes import AgentKind, AgentOutcome
from nauro.store.write_safety import find_file_symlink


def _claude_agent_dir() -> Path:
    return Path.home() / ".claude" / "agents"


def _codex_agent_dir() -> Path:
    return Path.home() / ".codex" / "agents"


def _agent_target(surface: str, name: str) -> Path:
    if surface == "claude_code":
        return _claude_agent_dir() / f"{name}.md"
    if surface == "codex":
        return _codex_agent_dir() / f"{name}.toml"
    raise ValueError(f"unknown surface: {surface!r}")


# Subagents are rendered from canonical bodies in nauro.agents and written into
# the user's surface directories: ``~/.claude/agents/`` for Claude Code and
# ``~/.codex/agents/`` for Codex.
# Unlike skills, agents are namespaced (``nauro-*``) and opt-in. The
# ``nauro-`` namespace is bundle-owned: on install, the current bundle
# wins, so a published body change (e.g. dropping a removed MCP tool) actually
# reaches users who installed an earlier version. A pre-existing
# A bundled agent file that differs from the current render is refreshed; its
# prior content is stashed to a sibling ``.bak`` so the rare hand-customization is
# recoverable. ``force_overwrite=True`` skips the ``.bak`` and overwrites in
# place. User-authored files without the ``nauro-`` prefix (e.g. a personal
# ``~/.claude/agents/planner.md``) are never touched.


def materialize_agents(
    surface: str,
    *,
    remove: bool,
    force_overwrite: bool = False,
    clear_user_scope: bool = True,
) -> list[AgentOutcome]:
    """Install or remove the bundled ``nauro-*`` subagent files.

    Claude Code and Codex are implemented. Cursor emits a single "skipped"
    line rather than crashing.

    Add path (per agent):
      - file absent → write bundled body.
      - file present and byte-equal → no-op.
      - file present and differs → refresh from the bundle, stashing the prior
        content to a sibling ``.bak`` (the nauro-* namespace is bundle-owned,
        so a differing file is almost always a stale earlier bundle). Pass
        ``force_overwrite=True`` to overwrite in place without the backup.

    Remove path (per agent):
      - file absent → skip.
      - file byte-equals bundled body → unlink.
      - file differs → preserve (locally modified).

    ``clear_user_scope`` mirrors the skill helpers: when False on the
    remove path, agents are preserved because other registered nauro
    projects still rely on them.
    """
    from nauro.agents import AGENT_NAMES, render_agent

    if surface == "cursor":
        try:
            render_agent(surface, AGENT_NAMES[0])
        except NotImplementedError:
            return [AgentOutcome(AgentKind.SURFACE_NOT_IMPLEMENTED, surface=surface)]
        except ValueError as exc:
            return [AgentOutcome(AgentKind.SURFACE_INVALID, surface=surface, detail=str(exc))]
    elif surface not in ("claude_code", "codex"):
        return [
            AgentOutcome(
                AgentKind.SURFACE_INVALID,
                surface=surface,
                detail=f"unknown surface: {surface!r}",
            )
        ]

    if remove and not clear_user_scope:
        return [AgentOutcome(AgentKind.PRESERVED, surface=surface)]

    results: list[AgentOutcome] = []
    for name in AGENT_NAMES:
        target = _agent_target(surface, name)
        refusal = find_file_symlink(target)
        if refusal is not None:
            results.append(AgentOutcome(AgentKind.REFUSED_SYMLINK, refusal=refusal))
            continue
        bundled = render_agent(surface, name)
        if remove:
            results.append(_remove_bundled_agent(target, bundled))
        else:
            results.append(_install_bundled_agent(target, bundled, force_overwrite=force_overwrite))
    return results


def _install_bundled_agent(target: Path, bundled: str, *, force_overwrite: bool) -> AgentOutcome:
    """Install or refresh one bundled agent file, returning its outcome.

    Absent → write the bundled body. Byte-equal → no-op. ``force_overwrite`` →
    overwrite in place. Otherwise the differing file is refreshed and its prior
    content stashed to a sibling ``.bak`` (unless that backup path is a refused
    symlink).
    """
    if target.is_file():
        current = target.read_text(encoding="utf-8")
        if current == bundled:
            return AgentOutcome(AgentKind.UNCHANGED, target=target)
        if force_overwrite:
            target.write_text(bundled, encoding="utf-8")
            return AgentOutcome(AgentKind.OVERWROTE, target=target)
        backup = target.parent / (target.name + ".bak")
        backup_refusal = find_file_symlink(backup)
        if backup_refusal is not None:
            return AgentOutcome(AgentKind.REFUSED_SYMLINK, refusal=backup_refusal)
        backup.write_text(current, encoding="utf-8")
        target.write_text(bundled, encoding="utf-8")
        return AgentOutcome(AgentKind.UPDATED, target=target, backup_name=backup.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bundled, encoding="utf-8")
    return AgentOutcome(AgentKind.INSTALLED, target=target)


def _remove_bundled_agent(target: Path, bundled: str) -> AgentOutcome:
    """Remove one bundled agent file, returning its outcome.

    Absent → skip note. Byte-equal to the bundle → unlink. Differs → preserve
    (locally modified).
    """
    if not target.is_file():
        return AgentOutcome(AgentKind.ABSENT, target=target)
    current = target.read_text(encoding="utf-8")
    if current == bundled:
        target.unlink()
        return AgentOutcome(AgentKind.REMOVED, target=target)
    return AgentOutcome(AgentKind.PRESERVED_MODIFIED, target=target)
