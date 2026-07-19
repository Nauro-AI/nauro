"""Skill artifact codec for the setup surface."""

from __future__ import annotations

from pathlib import Path

from nauro.cli.integrations.outcomes import SkillKind, SkillOutcome
from nauro.store.write_safety import (
    SymlinkRefusal,
    UserSymlinkRefusal,
    find_file_symlink,
    find_symlink,
)

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


def _skill_refusal(target: Path, repo: Path | None) -> SymlinkRefusal | UserSymlinkRefusal | None:
    if repo is None:
        return find_file_symlink(target)
    return find_symlink(repo, target.relative_to(repo).as_posix())


def _materialize_skill_file(target: Path, content: str) -> SkillOutcome:
    """Write an arbitrary user-scope skill file for compatibility callers."""
    refusal = find_file_symlink(target)
    if refusal is not None:
        return SkillOutcome(SkillKind.REFUSED_SYMLINK, target=target, refusal=refusal)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return SkillOutcome(SkillKind.WROTE, target=target)


def _install_bundled_skill(
    target: Path,
    bundled: str,
    *,
    force_overwrite: bool,
    repo: Path | None = None,
) -> SkillOutcome:
    refusal = _skill_refusal(target, repo)
    if refusal is not None:
        return SkillOutcome(SkillKind.REFUSED_SYMLINK, target=target, refusal=refusal, repo=repo)

    if target.is_file():
        current = target.read_text(encoding="utf-8")
        if current == bundled:
            return SkillOutcome(SkillKind.UNCHANGED, target=target, repo=repo)
        if force_overwrite:
            target.write_text(bundled, encoding="utf-8")
            return SkillOutcome(SkillKind.OVERWROTE, target=target, repo=repo)
        backup = target.with_name(target.name + ".bak")
        backup_refusal = _skill_refusal(backup, repo)
        if backup_refusal is not None:
            return SkillOutcome(
                SkillKind.REFUSED_SYMLINK,
                target=target,
                refusal=backup_refusal,
                repo=repo,
            )
        backup.write_text(current, encoding="utf-8")
        target.write_text(bundled, encoding="utf-8")
        return SkillOutcome(
            SkillKind.UPDATED,
            target=target,
            repo=repo,
            backup_name=backup.name,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bundled, encoding="utf-8")
    return SkillOutcome(SkillKind.WROTE, target=target, repo=repo)


def _remove_bundled_skill(
    target: Path,
    bundled: str,
    *,
    stop_above: Path,
    repo: Path | None = None,
) -> SkillOutcome:
    """Unlink ``target`` and prune empty parents, but never above ``stop_above``.

    Without the bound the parent walk could rmdir the surface base
    (``~/.claude/skills/``, ``<repo>/.cursor/rules/``, etc.) or — worse —
    keep going if those happen to be empty after MCP config also got removed.
    """
    refusal = _skill_refusal(target, repo)
    if refusal is not None:
        return SkillOutcome(SkillKind.REFUSED_SYMLINK, target=target, refusal=refusal, repo=repo)
    if not target.is_file():
        return SkillOutcome(SkillKind.ABSENT, target=target, repo=repo)
    if target.read_text(encoding="utf-8") != bundled:
        return SkillOutcome(SkillKind.PRESERVED_MODIFIED, target=target, repo=repo)
    target.unlink()
    stop_resolved = stop_above.resolve()
    parent = target.parent
    while parent.is_dir() and not any(parent.iterdir()):
        if parent.resolve() == stop_resolved:
            break
        parent.rmdir()
        parent = parent.parent
    return SkillOutcome(SkillKind.REMOVED, target=target, repo=repo)


def _remove_skill_file(target: Path, *, stop_above: Path) -> SkillOutcome:
    """Remove an arbitrary skill file for compatibility with codec callers.

    Bundle teardown uses ``_remove_bundled_skill`` so modified Nauro files are
    preserved. This lower-level helper retains the original bounded-prune
    contract for callers that already selected an exact disposable target.
    """
    refusal = find_file_symlink(target)
    if refusal is not None:
        return SkillOutcome(SkillKind.REFUSED_SYMLINK, target=target, refusal=refusal)
    if not target.is_file():
        return SkillOutcome(SkillKind.ABSENT, target=target)
    target.unlink()
    stop_resolved = stop_above.resolve()
    parent = target.parent
    while parent.is_dir() and not any(parent.iterdir()):
        if parent.resolve() == stop_resolved:
            break
        parent.rmdir()
        parent = parent.parent
    return SkillOutcome(SkillKind.REMOVED, target=target)


def _migrate_legacy_codex_skill(name: str) -> SkillOutcome | None:
    source = Path.home() / ".codex" / "skills" / name
    if source.is_symlink():
        return SkillOutcome(
            SkillKind.REFUSED_SYMLINK,
            source=source,
            refusal=UserSymlinkRefusal(source),
        )
    skill_file = source / "SKILL.md"
    if skill_file.is_symlink():
        return SkillOutcome(
            SkillKind.REFUSED_SYMLINK,
            source=source,
            refusal=UserSymlinkRefusal(skill_file),
        )
    if not skill_file.is_file():
        return None

    backup_root = Path.home() / ".codex" / "nauro-skill-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / name
    suffix = 1
    while backup.exists() or backup.is_symlink():
        backup = backup_root / f"{name}.{suffix}"
        suffix += 1
    source.rename(backup)
    return SkillOutcome(
        SkillKind.MIGRATED_LEGACY,
        source=source,
        backup_path=backup,
    )


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
    force_overwrite: bool = False,
) -> list[SkillOutcome]:
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
        return [SkillOutcome(SkillKind.PRESERVED, base_label="~/.claude/skills")]

    results: list[SkillOutcome] = []
    for name in _resolved_skill_names(with_skills):
        target = base / name / "SKILL.md"
        bundled = render_skill("claude_code", name)
        if remove:
            results.append(_remove_bundled_skill(target, bundled, stop_above=base))
        else:
            results.append(
                _install_bundled_skill(
                    target,
                    bundled,
                    force_overwrite=force_overwrite,
                )
            )
    return results


def materialize_skills_codex(
    *,
    remove: bool,
    clear_user_scope: bool = True,
    with_skills: bool = False,
    force_overwrite: bool = False,
) -> list[SkillOutcome]:
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
        return [SkillOutcome(SkillKind.PRESERVED, base_label="~/.agents/skills")]

    results: list[SkillOutcome] = []
    for name in _resolved_skill_names(with_skills):
        target = base / name / "SKILL.md"
        bundled = render_skill("codex", name)
        if remove:
            results.append(_remove_bundled_skill(target, bundled, stop_above=base))
        else:
            installed = _install_bundled_skill(
                target,
                bundled,
                force_overwrite=force_overwrite,
            )
            results.append(installed)
            if installed.kind is not SkillKind.REFUSED_SYMLINK:
                migrated = _migrate_legacy_codex_skill(name)
                if migrated is not None:
                    results.append(migrated)
    return results


def materialize_skills_cursor_for_repo(
    repo: Path,
    *,
    remove: bool,
    with_skills: bool = False,
    force_overwrite: bool = False,
) -> list[SkillOutcome]:
    """Install or remove Cursor rules under ``<repo>/.cursor/rules/``.

    ``with_skills`` extends the install/remove set with ``OPT_IN_SKILL_NAMES``.
    """
    from nauro.skills import render_skill

    base = repo / ".cursor" / "rules"
    results: list[SkillOutcome] = []
    for name in _resolved_skill_names(with_skills):
        target = base / f"{name}.mdc"
        bundled = render_skill("cursor", name)
        if remove:
            results.append(
                _remove_bundled_skill(
                    target,
                    bundled,
                    stop_above=base,
                    repo=repo,
                )
            )
        else:
            results.append(
                _install_bundled_skill(
                    target,
                    bundled,
                    force_overwrite=force_overwrite,
                    repo=repo,
                )
            )
    return results
