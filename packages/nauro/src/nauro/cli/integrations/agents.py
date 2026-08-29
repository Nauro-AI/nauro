"""Agent (subagent) artifact codec for the setup surface."""

from __future__ import annotations

from pathlib import Path

from nauro.cli.integrations.outcomes import AgentKind, AgentOutcome
from nauro.store.write_safety import (
    SymlinkRefusal,
    UserSymlinkRefusal,
    find_file_symlink,
    find_symlink,
)


def _claude_agent_dir() -> Path:
    return Path.home() / ".claude" / "agents"


def _codex_agent_dir() -> Path:
    return Path.home() / ".codex" / "agents"


def _agent_dir(surface: str) -> Path:
    if surface == "claude_code":
        return _claude_agent_dir()
    if surface == "codex":
        return _codex_agent_dir()
    raise ValueError(f"unknown surface: {surface!r}")


def _agent_extension(surface: str) -> str:
    if surface in ("claude_code", "cursor"):
        return ".md"
    if surface == "codex":
        return ".toml"
    raise ValueError(f"unknown surface: {surface!r}")


def materialize_agents(
    surface: str,
    *,
    remove: bool,
    force_overwrite: bool = False,
    clear_user_scope: bool = True,
) -> list[AgentOutcome]:
    """Install or remove user-global Claude Code or Codex ``nauro-*`` subagent files.
    Add writes an absent file and stashes a differing one to ``.bak``, unless ``force_overwrite``.
    Remove unlinks byte-equal files only; without ``clear_user_scope`` it preserves them all.
    """
    if surface not in ("claude_code", "codex"):
        return [
            AgentOutcome(
                AgentKind.SURFACE_INVALID,
                surface=surface,
                detail=f"unknown surface: {surface!r}",
            )
        ]

    if remove and not clear_user_scope:
        return [AgentOutcome(AgentKind.PRESERVED, surface=surface)]

    return _materialize_agent_files(
        surface,
        base=_agent_dir(surface),
        remove=remove,
        force_overwrite=force_overwrite,
    )


def materialize_agents_cursor_for_repo(
    repo: Path,
    *,
    remove: bool,
    force_overwrite: bool = False,
) -> list[AgentOutcome]:
    """Install or remove Cursor workflow agents under ``<repo>/.cursor/agents/``."""
    return _materialize_agent_files(
        "cursor",
        base=repo / ".cursor" / "agents",
        remove=remove,
        force_overwrite=force_overwrite,
        repo=repo,
    )


def _materialize_agent_files(
    surface: str,
    *,
    base: Path,
    remove: bool,
    force_overwrite: bool,
    repo: Path | None = None,
) -> list[AgentOutcome]:
    """Materialize one surface's agent set with scope-appropriate symlink checks."""
    from nauro.agents import AGENT_NAMES, render_agent

    extension = _agent_extension(surface)
    results: list[AgentOutcome] = []
    for name in AGENT_NAMES:
        target = base / f"{name}{extension}"
        refusal = _agent_refusal(target, repo=repo)
        if refusal is not None:
            results.append(AgentOutcome(AgentKind.REFUSED_SYMLINK, refusal=refusal))
            continue
        bundled = render_agent(surface, name)
        if remove:
            results.append(_remove_bundled_agent(target, bundled))
        else:
            results.append(
                _install_bundled_agent(
                    target,
                    bundled,
                    force_overwrite=force_overwrite,
                    repo=repo,
                )
            )
    return results


def _agent_refusal(
    target: Path, *, repo: Path | None
) -> SymlinkRefusal | UserSymlinkRefusal | None:
    if repo is None:
        return find_file_symlink(target)
    return find_symlink(repo, target.relative_to(repo).as_posix())


def _install_bundled_agent(
    target: Path,
    bundled: str,
    *,
    force_overwrite: bool,
    repo: Path | None = None,
) -> AgentOutcome:
    """Install or refresh one bundled agent file, returning its outcome.
    Absent writes the body, byte-equal is a no-op, ``force_overwrite`` overwrites in place, and a
    differing file is refreshed with its prior content stashed to a sibling ``.bak``.
    """
    if target.is_file():
        current = target.read_text(encoding="utf-8")
        if current == bundled:
            return AgentOutcome(AgentKind.UNCHANGED, target=target)
        if force_overwrite:
            target.write_text(bundled, encoding="utf-8")
            return AgentOutcome(AgentKind.OVERWROTE, target=target)
        backup = target.parent / (target.name + ".bak")
        backup_refusal = _agent_refusal(backup, repo=repo)
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
    Absent skips, byte-equal to the bundle unlinks, and a differing file is preserved.
    """
    if not target.is_file():
        return AgentOutcome(AgentKind.ABSENT, target=target)
    current = target.read_text(encoding="utf-8")
    if current == bundled:
        target.unlink()
        return AgentOutcome(AgentKind.REMOVED, target=target)
    return AgentOutcome(AgentKind.PRESERVED_MODIFIED, target=target)
