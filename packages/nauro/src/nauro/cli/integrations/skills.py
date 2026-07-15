"""Skill artifact codec for the setup surface."""

from __future__ import annotations

from pathlib import Path

from nauro.store.write_safety import find_file_symlink, find_symlink

# Skills are rendered from canonical bodies in nauro.skills and written into
# the user's surface directories. Claude Code and Codex skills are user-global;
# Cursor skills ship per-project (Cursor's "User Rules" live in the IDE
# Settings UI, not a file path).
#
# ``SKILL_NAMES`` is the always-installed set — the core onboarding skill.
# ``OPT_IN_SKILL_NAMES`` is materialized only when the caller passes
# ``with_skills=True``. ``nauro-ship-task`` references the bundled ``@nauro-*``
# subagents and is opt-in for that reason, so the ``--with-subagents`` notice
# stays scoped to it. ``nauro-loop`` dispatches ``/nauro-ship-task``
# byte-for-byte, so it carries the same subagent dependency transitively.
# ``nauro-context`` composes only existing MCP tools (plus the agent's own
# filesystem write) with no subagent dependency, so it carries no such notice.

SKILL_NAMES: tuple[str, ...] = ("nauro-adopt",)
OPT_IN_SKILL_NAMES: tuple[str, ...] = ("nauro-ship-task", "nauro-context", "nauro-loop")


def _claude_skill_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def _codex_skill_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def _materialize_skill_file(target: Path, content: str) -> str:
    refusal = find_file_symlink(target)
    if refusal is not None:
        return f"  {refusal.message}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"  wrote {target}"


def _remove_skill_file(target: Path, *, stop_above: Path) -> str:
    """Unlink ``target`` and prune empty parents, but never above ``stop_above``.

    Without the bound the parent walk could rmdir the surface base
    (``~/.claude/skills/``, ``<repo>/.cursor/rules/``, etc.) or — worse —
    keep going if those happen to be empty after MCP config also got removed.
    """
    refusal = find_file_symlink(target)
    if refusal is not None:
        return f"  {refusal.message}"
    if not target.is_file():
        return f"  no skill at {target}"
    target.unlink()
    stop_resolved = stop_above.resolve()
    parent = target.parent
    while parent.is_dir() and not any(parent.iterdir()):
        if parent.resolve() == stop_resolved:
            break
        parent.rmdir()
        parent = parent.parent
    return f"  removed {target}"


def _resolved_skill_names(with_skills: bool) -> tuple[str, ...]:
    """Return the union of always-installed skills and opt-in skills.

    ``with_skills=False`` (the default for callers that pre-date the flag)
    installs only the core onboarding skills in ``SKILL_NAMES``.
    ``with_skills=True`` extends with ``OPT_IN_SKILL_NAMES`` so future opt-in
    skills can ride alongside ``nauro-ship-task`` under the same flag.
    """
    return SKILL_NAMES + OPT_IN_SKILL_NAMES if with_skills else SKILL_NAMES


def materialize_skills_claude_code(
    *,
    remove: bool,
    clear_user_scope: bool = True,
    with_skills: bool = False,
) -> list[str]:
    """Install or remove the Nauro skill(s) under ``~/.claude/skills/``.

    ``clear_user_scope`` gates the remove path: when False, the skill files
    are preserved because other registered nauro projects still depend on
    them. Defaults to True so direct unit callers and the add path retain
    their previous behavior. ``with_skills`` extends the install/remove set
    with ``OPT_IN_SKILL_NAMES`` (``nauro-ship-task``, ``nauro-context``, and
    ``nauro-loop``).
    """
    from nauro.skills import render_skill

    base = _claude_skill_dir()
    if remove and not clear_user_scope:
        return ["  preserved ~/.claude/skills/nauro-* (other nauro projects still registered)"]

    results: list[str] = []
    for name in _resolved_skill_names(with_skills):
        target = base / name / "SKILL.md"
        if remove:
            results.append(_remove_skill_file(target, stop_above=base))
        else:
            results.append(_materialize_skill_file(target, render_skill("claude_code", name)))
    return results


def materialize_skills_codex(
    *,
    remove: bool,
    clear_user_scope: bool = True,
    with_skills: bool = False,
) -> list[str]:
    """Install or remove the Nauro skill(s) under ``~/.agents/skills/``.

    ``clear_user_scope`` gates the remove path: when False, the skill files
    are preserved because other registered nauro projects still depend on
    them. Defaults to True so direct unit callers and the add path retain
    their previous behavior. ``with_skills`` extends the install/remove set
    with ``OPT_IN_SKILL_NAMES`` (``nauro-ship-task``, ``nauro-context``, and
    ``nauro-loop``).
    """
    from nauro.skills import render_skill

    base = _codex_skill_dir()
    if remove and not clear_user_scope:
        return ["  preserved ~/.agents/skills/nauro-* (other nauro projects still registered)"]

    results: list[str] = []
    for name in _resolved_skill_names(with_skills):
        target = base / name / "SKILL.md"
        if remove:
            results.append(_remove_skill_file(target, stop_above=base))
        else:
            results.append(_materialize_skill_file(target, render_skill("codex", name)))
    return results


def materialize_skills_cursor_for_repo(
    repo: Path,
    *,
    remove: bool,
    with_skills: bool = False,
) -> list[str]:
    """Install or remove Cursor rules under ``<repo>/.cursor/rules/``.

    ``with_skills`` extends the install/remove set with ``OPT_IN_SKILL_NAMES``.
    """
    from nauro.skills import render_skill

    base = repo / ".cursor" / "rules"
    results: list[str] = []
    for name in _resolved_skill_names(with_skills):
        refusal = find_symlink(repo, f".cursor/rules/{name}.mdc")
        if refusal is not None:
            results.append(f"  {repo}: {refusal.message}")
            continue
        target = base / f"{name}.mdc"
        if remove:
            results.append(_remove_skill_file(target, stop_above=base))
        else:
            results.append(_materialize_skill_file(target, render_skill("cursor", name)))
    return results
