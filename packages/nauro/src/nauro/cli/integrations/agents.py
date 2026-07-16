"""Agent (subagent) artifact codec for the setup surface."""

from __future__ import annotations

from pathlib import Path

from nauro.store.write_safety import find_file_symlink


def _claude_agent_dir() -> Path:
    return Path.home() / ".claude" / "agents"


# Subagents are rendered from canonical bodies in nauro.agents and written into
# the user's surface directories. On Claude Code, that's ``~/.claude/agents/``.
# Unlike skills, agents are namespaced (``nauro-*``) and opt-in. The
# ``nauro-`` namespace is bundle-owned: on install, the current bundle
# wins, so a published body change (e.g. dropping a removed MCP tool) actually
# reaches users who installed an earlier version. A pre-existing
# ``nauro-<name>.md`` that differs from the bundle is refreshed; its prior
# content is stashed to ``<name>.md.bak`` so the rare hand-customization is
# recoverable. ``force_overwrite=True`` skips the ``.bak`` and overwrites in
# place. User-authored files without the ``nauro-`` prefix (e.g. a personal
# ``~/.claude/agents/planner.md``) are never touched.


def materialize_agents(
    surface: str,
    *,
    remove: bool,
    force_overwrite: bool = False,
    clear_user_scope: bool = True,
) -> list[str]:
    """Install or remove the bundled ``nauro-*`` subagent files.

    Currently only the Claude Code surface is implemented. Cursor and Codex
    surfaces emit a single "skipped" line rather than crashing so the
    install path can call this unconditionally per the user's flag choice.

    Add path (per agent):
      - file absent → write bundled body.
      - file present and byte-equal → no-op.
      - file present and differs → refresh from the bundle, stashing the prior
        content to ``<name>.md.bak`` (the nauro-* namespace is bundle-owned, so
        a differing file is almost always a stale earlier bundle). Pass
        ``force_overwrite=True`` to overwrite in place without the ``.bak``.

    Remove path (per agent):
      - file absent → skip.
      - file byte-equals bundled body → unlink.
      - file differs → preserve (locally modified).

    ``clear_user_scope`` mirrors the skill helpers: when False on the
    remove path, agents are preserved because other registered nauro
    projects still rely on them.
    """
    from nauro.agents import AGENT_NAMES, render_agent

    if surface != "claude_code":
        try:
            # Exercise the stub so a future surface implementation doesn't
            # need to remember to remove this branch — once render_agent
            # stops raising, the stub message goes away naturally.
            render_agent(surface, AGENT_NAMES[0])
        except NotImplementedError:
            return [f"  skipped ~/.{surface} agents (not yet implemented)"]
        except ValueError as exc:
            return [f"  skipped agents on surface {surface!r}: {exc}"]

    base = _claude_agent_dir()
    if remove and not clear_user_scope:
        return ["  preserved ~/.claude/agents/nauro-* (other nauro projects still registered)"]

    results: list[str] = []
    for name in AGENT_NAMES:
        target = base / f"{name}.md"
        refusal = find_file_symlink(target)
        if refusal is not None:
            results.append(f"  {refusal.message}")
            continue
        bundled = render_agent("claude_code", name)
        if remove:
            results.append(_remove_bundled_agent(target, bundled))
        else:
            results.append(_install_bundled_agent(target, bundled, force_overwrite=force_overwrite))
    return results


def _install_bundled_agent(target: Path, bundled: str, *, force_overwrite: bool) -> str:
    """Install or refresh one bundled agent file, returning its status line.

    Absent → write the bundled body. Byte-equal → no-op. ``force_overwrite`` →
    overwrite in place. Otherwise the differing file is refreshed and its prior
    content stashed to ``<name>.md.bak`` (unless that backup path is a refused
    symlink).
    """
    if target.is_file():
        current = target.read_text(encoding="utf-8")
        if current == bundled:
            return f"  unchanged {target}"
        if force_overwrite:
            target.write_text(bundled, encoding="utf-8")
            return f"  overwrote {target}"
        backup = target.parent / (target.name + ".bak")
        backup_refusal = find_file_symlink(backup)
        if backup_refusal is not None:
            return f"  {backup_refusal.message}"
        backup.write_text(current, encoding="utf-8")
        target.write_text(bundled, encoding="utf-8")
        return f"  updated {target} (previous saved to {backup.name})"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bundled, encoding="utf-8")
    return f"  installed {target}"


def _remove_bundled_agent(target: Path, bundled: str) -> str:
    """Remove one bundled agent file, returning its status line.

    Absent → skip note. Byte-equal to the bundle → unlink. Differs → preserve
    (locally modified).
    """
    if not target.is_file():
        return f"  no agent at {target}"
    current = target.read_text(encoding="utf-8")
    if current == bundled:
        target.unlink()
        return f"  removed {target}"
    return f"  preserved {target} (locally modified)"
