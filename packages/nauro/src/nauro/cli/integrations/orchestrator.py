"""Cross-surface setup orchestration policy shared by the setup commands and adopt."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nauro.cli.integrations.agents import materialize_agents
from nauro.cli.integrations.claude_hooks import materialize_hooks_claude_code
from nauro.cli.integrations.claude_user_config import _prune_redundant_user_scope_mcp
from nauro.cli.integrations.codex_config import _configure_codex, _default_codex_config_path
from nauro.cli.integrations.codex_hooks import _nearest_codex_hooks_repo, materialize_hooks_codex
from nauro.cli.integrations.json_mcp import _configure_cursor_for_repo, _configure_mcp
from nauro.cli.integrations.legacy import _remove_claude_md
from nauro.cli.integrations.outcomes import ArtifactOutcome, RawLine
from nauro.cli.integrations.skills import (
    materialize_skills_claude_code,
    materialize_skills_codex,
    materialize_skills_cursor_for_repo,
)
from nauro.cli.integrations.user_scope import _registered_project_keys, _user_scope_safe_to_clear
from nauro.cli.utils import _resolve_project_entry, resolve_target_project
from nauro.store.registry import get_repo_paths
from nauro.store.resolution import resolve_from_cwd
from nauro.templates.agents_md import remove_generated_agents_md
from nauro.templates.agents_md_regen import warn_then_regen


def claude_code_surfaces(
    project_repos: list[Path],
    *,
    remove: bool,
    with_hooks: bool,
    store_name: str,
    store_path: Path,
    warn: Callable[[str], None],
) -> list[ArtifactOutcome]:
    """Wire Claude Code MCP + hooks per repo and regenerate AGENTS.md.

    Returns the flat status lines the command echoes to stdout, in order:
    the per-repo MCP lines (plus the user-scope prune note on add), then a
    ``Hooks:`` section, a ``Legacy cleanup:`` section, and an ``AGENTS.md:``
    section when each has data. Skip and git-hygiene warnings from
    ``warn_then_regen`` route through ``warn`` (stderr), never the returned
    list.
    """
    legacy_results: list[str] = []
    mcp_results: list[str] = []
    hook_results: list[str] = []
    for repo_path in project_repos:
        if not repo_path.is_dir():
            mcp_results.append(f"  {repo_path}: repo path missing, skipped")
            continue
        legacy = _remove_claude_md(repo_path)
        if legacy:
            legacy_results.append(legacy)
        mcp_results.append(_configure_mcp(repo_path, remove=remove))
        if with_hooks or remove:
            try:
                hook_results.append(materialize_hooks_claude_code(repo_path, remove=remove))
            except Exception as exc:
                hook_results.append(f"  {repo_path}: hook wiring error - {exc}")

    if not remove:
        pruned = _prune_redundant_user_scope_mcp()
        if pruned:
            mcp_results.append(pruned)

    outcomes: list[ArtifactOutcome] = [RawLine(result) for result in mcp_results]

    if hook_results:
        outcomes.append(RawLine("\nHooks:"))
        outcomes.extend(RawLine(result) for result in hook_results)

    if legacy_results:
        outcomes.append(RawLine("\nLegacy cleanup:"))
        outcomes.extend(RawLine(result) for result in legacy_results)

    if not remove:
        # Regenerate AGENTS.md so context is fresh from the start. The store
        # dir name is the v2 id (or v1 name) used by the registry-aware lookup.
        # warn_then_regen surfaces missing-repo, symlink-refusal, and
        # git-hygiene warnings through the warn callback (stderr), never the
        # returned outcomes.
        updated_repos = warn_then_regen(store_name, store_path, warn=warn)
        if updated_repos:
            outcomes.append(RawLine("\nAGENTS.md:"))
            for repo_path in updated_repos:
                outcomes.append(RawLine(f"  {repo_path}: regenerated AGENTS.md"))

    return outcomes


def cursor_surfaces(project_repos: list[Path], *, remove: bool) -> list[ArtifactOutcome]:
    """Wire Cursor MCP per repo. Returns the per-repo status lines."""
    outcomes: list[ArtifactOutcome] = []
    for repo_path in project_repos:
        if not repo_path.is_dir():
            outcomes.append(RawLine(f"  {repo_path}: repo path missing, skipped"))
            continue
        outcomes.append(RawLine(_configure_cursor_for_repo(repo_path, remove=remove)))
    return outcomes


def codex_surfaces(*, remove: bool, with_hooks: bool) -> list[ArtifactOutcome]:
    """Wire the user-global Codex MCP entry and per-repo Codex hooks.

    Resolves the project internally (Codex config is user-global and shared by
    every registered project). Returns the flat status lines the command
    echoes: the config line, then a ``Hooks:`` section when hooks are wired or
    torn down.
    """
    hook_repos: list[Path] = []
    if with_hooks and not remove:
        project_name, store_path = resolve_target_project(None)
        entry = _resolve_project_entry(project_name, store_path.name)
        hook_repos = [Path(repo_path) for repo_path in entry["repo_paths"]]

    outcomes: list[ArtifactOutcome] = []

    # Standalone codex wiring is user-global and shared by every registered
    # project, so this teardown preserves the entry while any project remains
    # in the registry (it clears only on an empty registry). Clearing on the
    # last project goes through 'nauro setup all --remove'.
    registered_count = len(_registered_project_keys()) if remove else 0
    if registered_count:
        config_path = _default_codex_config_path()
        count_phrase = (
            "1 nauro project" if registered_count == 1 else f"{registered_count} nauro projects"
        )
        outcomes.append(
            RawLine(
                f"Codex: preserved nauro entry in {config_path} ({count_phrase} registered; "
                "run 'nauro setup all --remove' on the last project to clear this "
                "user-global entry)"
            )
        )
    else:
        outcomes.append(RawLine(_configure_codex(remove=remove)))

    hook_cleanup_unresolved = False
    if remove:
        try:
            resolution = resolve_from_cwd(Path.cwd())
            hook_repos = (
                [Path(repo_path) for repo_path in get_repo_paths(resolution.project_id)]
                if resolution is not None
                else []
            )
            nearest_hooks_repo = _nearest_codex_hooks_repo(Path.cwd())
            if nearest_hooks_repo is not None and nearest_hooks_repo not in hook_repos:
                hook_repos.append(nearest_hooks_repo)
            hook_cleanup_unresolved = not hook_repos
        except Exception:
            hook_cleanup_unresolved = True

    if with_hooks or remove:
        outcomes.append(RawLine("\nHooks:"))
        if hook_cleanup_unresolved:
            outcomes.append(
                RawLine(
                    "  Project-scoped Codex hooks were not removed because no Nauro "
                    "project resolves from this directory. Run this command from each "
                    "wired repo to remove them."
                )
            )
        for repo_path in hook_repos:
            if not repo_path.is_dir():
                outcomes.append(RawLine(f"  {repo_path}: repo path missing, skipped"))
                continue
            try:
                outcomes.append(RawLine(materialize_hooks_codex(repo_path, remove=remove)))
            except Exception as exc:
                outcomes.append(RawLine(f"  {repo_path}: Codex hook wiring error - {exc}"))

    return outcomes


def _all_claude_code_lines(
    project_repos: list[Path],
    *,
    remove: bool,
    clear_user_scope: bool,
    with_subagents: bool,
    force_overwrite: bool,
    with_skills: bool,
    with_hooks: bool,
) -> list[ArtifactOutcome]:
    """Claude Code surface lines for ``setup_all_surfaces``.

    MCP per-repo (direct ``.mcp.json`` write), user-scope prune on add, skills
    global, optional subagents, then the per-repo advisory hook.
    """
    outcomes: list[ArtifactOutcome] = []
    for repo in project_repos:
        if not repo.is_dir():
            outcomes.append(RawLine(f"  {repo}: repo path missing, skipped"))
            continue
        try:
            outcomes.append(RawLine(_configure_mcp(repo, remove=remove)))
        except Exception as exc:
            outcomes.append(RawLine(f"Claude Code MCP ({repo}): error - {exc}"))
    if not remove:
        try:
            pruned = _prune_redundant_user_scope_mcp()
            if pruned:
                outcomes.append(RawLine(pruned))
        except Exception as exc:  # never let cleanup break wiring
            outcomes.append(RawLine(f"Claude Code MCP (user-scope cleanup): error - {exc}"))
    try:
        outcomes.extend(
            RawLine(line)
            for line in materialize_skills_claude_code(
                remove=remove,
                clear_user_scope=clear_user_scope,
                with_skills=with_skills,
            )
        )
    except Exception as exc:
        outcomes.append(RawLine(f"Claude Code skills: error - {exc}"))

    if with_subagents:
        try:
            outcomes.extend(
                RawLine(line)
                for line in materialize_agents(
                    "claude_code",
                    remove=remove,
                    force_overwrite=force_overwrite,
                    clear_user_scope=clear_user_scope,
                )
            )
        except Exception as exc:
            outcomes.append(RawLine(f"Claude Code agents: error - {exc}"))

    if with_hooks or remove:
        for repo in project_repos:
            if not repo.is_dir():
                continue
            try:
                outcomes.append(RawLine(materialize_hooks_claude_code(repo, remove=remove)))
            except Exception as exc:
                outcomes.append(RawLine(f"Claude Code hook ({repo}): error - {exc}"))
    return outcomes


def _all_cursor_lines(
    project_repos: list[Path], *, remove: bool, with_skills: bool
) -> list[ArtifactOutcome]:
    """Cursor surface lines for ``setup_all_surfaces``: MCP per repo + skills per repo."""
    outcomes: list[ArtifactOutcome] = []
    for repo in project_repos:
        if not repo.is_dir():
            continue
        try:
            outcomes.append(RawLine(_configure_cursor_for_repo(repo, remove=remove)))
        except Exception as exc:
            outcomes.append(RawLine(f"Cursor MCP ({repo}): error - {exc}"))
        try:
            outcomes.extend(
                RawLine(line)
                for line in materialize_skills_cursor_for_repo(
                    repo, remove=remove, with_skills=with_skills
                )
            )
        except Exception as exc:
            outcomes.append(RawLine(f"Cursor skills ({repo}): error - {exc}"))
    return outcomes


def _all_codex_lines(
    project_repos: list[Path],
    *,
    remove: bool,
    clear_user_scope: bool,
    with_skills: bool,
    with_hooks: bool,
) -> list[ArtifactOutcome]:
    """Codex surface lines for ``setup_all_surfaces``: MCP global, skills global, optional hooks."""
    outcomes: list[ArtifactOutcome] = []
    try:
        outcomes.append(RawLine(_configure_codex(remove=remove, clear_user_scope=clear_user_scope)))
    except Exception as exc:
        outcomes.append(RawLine(f"Codex MCP: error - {exc}"))
    try:
        outcomes.extend(
            RawLine(line)
            for line in materialize_skills_codex(
                remove=remove,
                clear_user_scope=clear_user_scope,
                with_skills=with_skills,
            )
        )
    except Exception as exc:
        outcomes.append(RawLine(f"Codex skills: error - {exc}"))

    if with_hooks or remove:
        for repo in project_repos:
            if not repo.is_dir():
                continue
            try:
                outcomes.append(RawLine(materialize_hooks_codex(repo, remove=remove)))
            except Exception as exc:
                outcomes.append(RawLine(f"Codex hooks ({repo}): error - {exc}"))
    return outcomes


def _all_agents_md_lines(
    project_repos: list[Path],
    *,
    remove: bool,
    current_project_key: str | None,
    store_path: Path | None,
) -> list[ArtifactOutcome]:
    """AGENTS.md regen (add) and teardown (remove) lines for ``setup_all_surfaces``.

    On the add path ``warn_then_regen`` routes missing-repo, symlink-refusal,
    and git-hygiene warnings into ``warn``; the callback appends into this
    helper's own returned list so those warnings surface on stdout in the same
    position the inline code produced them.
    """
    outcomes: list[ArtifactOutcome] = []
    # Regenerate AGENTS.md once so context is fresh from the start on every
    # entry point that wires surfaces. Guarded on the add path and on having a
    # store to read from. warn_then_regen routes missing-repo, symlink-refusal,
    # and git-hygiene warnings into the status lines.
    if not remove and current_project_key is not None and store_path is not None:
        try:
            updated = warn_then_regen(
                current_project_key,
                store_path,
                warn=lambda message: outcomes.append(RawLine(message)),
            )
            for repo_path in updated:
                outcomes.append(RawLine(f"  {repo_path}: regenerated AGENTS.md"))
        except Exception as exc:
            outcomes.append(RawLine(f"AGENTS.md regeneration: error - {exc}"))

    # Mirror of the regen above: strip the generated AGENTS.md on teardown so a
    # removed integration leaves no orphaned context file. User content in a
    # ``# Manual`` section is preserved (the file is kept) by the helper.
    if remove:
        for repo in project_repos:
            if not repo.is_dir():
                continue
            try:
                removed_line = remove_generated_agents_md(repo)
                if removed_line:
                    outcomes.append(RawLine(removed_line))
            except Exception as exc:
                outcomes.append(RawLine(f"AGENTS.md removal ({repo}) failed: {exc}"))
    return outcomes


def setup_all_surfaces(
    project_repos: list[Path],
    *,
    remove: bool = False,
    current_project_key: str | None = None,
    store_path: Path | None = None,
    with_subagents: bool = False,
    force_overwrite: bool = False,
    with_skills: bool = False,
    with_hooks: bool = False,
    clear_user_scope_override: bool | None = None,
) -> list[ArtifactOutcome]:
    """Wire MCP and materialize skills across Claude Code, Cursor, Codex.

    Continues across per-handler errors so partial coverage still reports
    progress. Returns the cumulative status lines.

    ``current_project_key`` is the registry key (v2 id or v1 name) for the
    project being wired or torn down. When ``remove=True``, it is excluded
    from the "are there other projects?" check so per-project teardown only
    clears user-scope artifacts (Claude/Codex skill, ``~/.codex/config.toml``)
    when this is the last project on the machine.

    On the add path, when both ``current_project_key`` and ``store_path`` are
    supplied, AGENTS.md is regenerated once across the project's repos so every
    entry point (``setup claude-code``, ``setup all``, ``adopt``) produces the
    cross-tool context file. The MCP-less Cursor/Codex surfaces depend on this
    fallback layer the most, yet only ``setup claude-code`` used to write it.

    ``with_subagents`` opts into installing or removing the bundled
    ``nauro-*`` workflow subagents under ``~/.claude/agents/``. Off by
    default so existing flows that pre-date the subagent bundle keep
    their previous behavior. ``force_overwrite`` is only meaningful when
    ``with_subagents`` is True and ``remove`` is False — it replaces
    locally-modified bundled files instead of preserving them.

    ``with_skills`` opts into installing the bundled opt-in skills
    (``nauro-ship-task``, ``nauro-context``, ``nauro-loop``). Independent of
    ``with_subagents`` so users
    can adopt skills and subagents on separate cadences, though
    ``nauro-ship-task`` references the bundled ``@nauro-*`` subagents in
    its body — and ``nauro-loop`` dispatches that chain — so a caller that
    surfaces ``with_skills`` without ``with_subagents`` should warn the user.

    ``with_hooks`` opts into wiring the advisory Claude Code
    ``UserPromptSubmit`` hook and the Codex ``SessionStart`` and
    ``SubagentStart`` hooks into each repo's project-scope configuration. Off
    by default. A hook-wiring failure is caught and reported as a status line
    so it never aborts the rest of setup.

    ``clear_user_scope_override`` forces the shared-user-scope decision instead
    of deriving it from the registry. ``nauro adopt --remove`` passes ``False``
    when it un-adopts one repo of a multi-repo project: the default
    ``_user_scope_safe_to_clear`` check is project-granular, so it would wrongly
    clear codex/skill/agent artifacts that the project's other repos still need.
    Leave ``None`` for the default behavior.
    """
    if clear_user_scope_override is not None:
        clear_user_scope = clear_user_scope_override
    else:
        clear_user_scope = _user_scope_safe_to_clear(current_project_key) if remove else True

    outcomes: list[ArtifactOutcome] = []
    outcomes.extend(
        _all_claude_code_lines(
            project_repos,
            remove=remove,
            clear_user_scope=clear_user_scope,
            with_subagents=with_subagents,
            force_overwrite=force_overwrite,
            with_skills=with_skills,
            with_hooks=with_hooks,
        )
    )
    outcomes.extend(_all_cursor_lines(project_repos, remove=remove, with_skills=with_skills))
    outcomes.extend(
        _all_codex_lines(
            project_repos,
            remove=remove,
            clear_user_scope=clear_user_scope,
            with_skills=with_skills,
            with_hooks=with_hooks,
        )
    )
    outcomes.extend(
        _all_agents_md_lines(
            project_repos,
            remove=remove,
            current_project_key=current_project_key,
            store_path=store_path,
        )
    )
    return outcomes


SHIP_TASK_NEEDS_SUBAGENTS_NOTICE = (
    "nauro-ship-task references the bundled @nauro-* subagents (and nauro-loop "
    "dispatches that chain); pass `--with-subagents` to install them too."
)

# The bundled subagents allow the cloud tools by the fixed name
# `mcp__claude_ai_Nauro__*`. That prefix only resolves when the remote
# connector is named exactly `Nauro`, so surface the requirement whenever
# subagents are installed.
SUBAGENTS_CONNECTOR_NAME_NOTICE = (
    "Cloud users: name the remote MCP connector exactly `Nauro` so the bundled "
    "@nauro-* subagents' `mcp__claude_ai_Nauro__*` tools resolve."
)
