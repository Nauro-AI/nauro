"""nauro setup — Configure tool integrations.

Subcommands:
  nauro setup claude-code  — register MCP server in <repo>/.mcp.json (project scope)
                             for each of the project's repos
  nauro setup cursor       — register MCP server in <repo>/.cursor/mcp.json
                             for each of the project's repos
  nauro setup codex        — register MCP server in ~/.codex/config.toml
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import typer

from nauro.cli.utils import _resolve_project_entry, resolve_target_project
from nauro.constants import CLAUDE_MD, NAURO_BLOCK_END, NAURO_BLOCK_START
from nauro.store.registry import (
    RegistrySchemaError,
    load_registry,
    load_registry_v2,
)
from nauro.templates.agents_md import regenerate_agents_md_for_project

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w

setup_app = typer.Typer(help="Configure tool integrations.")

# Legacy markers — kept for removal of old CLAUDE.md blocks during --remove.
CLAUDE_MD_START = NAURO_BLOCK_START
CLAUDE_MD_END = NAURO_BLOCK_END

# Discoverability hint appended to every setup-add success path.
# `nauro check-decision` (the L1 surface) works from the current shell
# against the local store, so users don't have to wait for an agent
# restart to see Nauro do something useful.
CHECK_HINT_LINE = 'Try it now from this shell: nauro check-decision "<approach>"'


def _remove_claude_md(repo_path: Path) -> str | None:
    """Remove a legacy Nauro block from CLAUDE.md if present.

    Returns a status string if a block was removed, or None if no block found.
    """
    claude_md = repo_path / CLAUDE_MD
    if not claude_md.exists():
        return None

    content = claude_md.read_text()
    if CLAUDE_MD_START not in content:
        return None

    before = content[: content.index(CLAUDE_MD_START)]
    after = content[content.index(CLAUDE_MD_END) + len(CLAUDE_MD_END) :]
    remaining = (before + after).strip()

    if not remaining:
        claude_md.unlink()
        return f"  {repo_path}: removed legacy Nauro block (deleted empty {CLAUDE_MD})"
    else:
        claude_md.write_text(remaining + "\n")
        return f"  {repo_path}: removed legacy Nauro block from {CLAUDE_MD}"


def _find_nauro_command() -> str:
    """Resolve an absolute path to the nauro entrypoint for MCP/hook configs.

    Prefer the console script next to the running interpreter — the install the
    user actually invoked, which a pip-into-venv or pipx/uv-tool layout often
    keeps off the PATH that Claude Code / Cursor / Codex launch with. Recording
    an absolute path keeps the spawned stdio server and the per-turn hook
    independent of the agent's launch environment. Fall back to a PATH lookup,
    then the bare name.
    """
    bindir = Path(sys.executable).parent
    for name in ("nauro", "nauro.exe"):
        candidate = bindir / name
        if candidate.is_file():
            return str(candidate)
    path = shutil.which("nauro")
    return path if path else "nauro"


def _user_scope_safe_to_clear(current_project_key: str | None) -> bool:
    """Return True iff no other nauro projects remain in the registry.

    User-scope artifacts (``~/.claude/skills/nauro-adopt``,
    ``~/.agents/skills/nauro-adopt``, and the ``nauro`` entry in
    ``~/.codex/config.toml``) are shared by every registered project on the
    machine, so a per-project teardown must not strip them while other
    projects still depend on them.
    """
    try:
        registry = load_registry_v2()
    except RegistrySchemaError:
        registry = load_registry()
    keys = set(registry.get("projects", {}).keys())
    if current_project_key is not None:
        keys.discard(current_project_key)
    return not keys


def _configure_json_mcp(
    repo_path: Path,
    *,
    config_rel_path: str,
    label: str,
    remove: bool,
) -> str:
    """Add or remove the Nauro MCP entry in a JSON config file at ``repo_path / config_rel_path``.

    Shared shape behind ``_configure_mcp`` (``.mcp.json``) and
    ``_configure_cursor_for_repo`` (``.cursor/mcp.json``): load → parse →
    mutate ``mcpServers["nauro"]`` → write. Both surfaces use the same key
    name and entry shape, so the only per-surface variation is the relative
    path and the human-readable ``label`` used in status messages.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    config_path = repo_path / config_rel_path
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as exc:
            return f"  {repo_path}: could not parse {label} — {exc}"
    else:
        config = {}

    # A hand-mangled config can have a non-object top level (e.g. a JSON array)
    # or an mcpServers that isn't an object; mutating it would raise. Skip with a
    # clear message instead of a traceback, mirroring the hook path's guard.
    if not isinstance(config, dict):
        return f"  {repo_path}: {label} is not a JSON object, skipped"

    if remove:
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict) or "nauro" not in servers:
            return f"  {repo_path}: no nauro entry to remove"
        del servers["nauro"]
        if not servers:
            config.pop("mcpServers", None)
        if config:
            config_path.write_text(json.dumps(config, indent=2) + "\n")
        else:
            config_path.unlink()
        return f"  {repo_path}: removed nauro from {label}"

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return f"  {repo_path}: mcpServers in {label} is not a JSON object, skipped"
    servers["nauro"] = nauro_entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return f"  {repo_path}: wrote nauro to {label}"


def _configure_mcp(repo_path: Path, *, remove: bool = False) -> str:
    """Add or remove the Nauro MCP entry in the repo's project-scope ``.mcp.json``.

    Writes the file directly. Mirrors how ``_configure_cursor_for_repo``
    handles ``.cursor/mcp.json`` and ``_configure_codex`` handles
    ``~/.codex/config.toml``, so all three surface handlers share one shape.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    return _configure_json_mcp(
        repo_path,
        config_rel_path=".mcp.json",
        label=".mcp.json",
        remove=remove,
    )


def _prune_redundant_user_scope_mcp() -> str | None:
    """Remove a redundant user-scope HTTP ``nauro`` entry from ``~/.claude.json``.

    On a machine with a local working copy, the stdio server is the canonical
    Claude Code transport: ``nauro serve --stdio`` resolves the store from the
    repo's ``.nauro/config.json`` and pulls remote changes on startup. An HTTP
    ``nauro`` entry in user-scope ``~/.claude.json`` collides with the
    project-scope stdio entry under the same name, so a session can resolve to
    the wrong store. When the project stdio entry is written, drop the
    redundant user-scope HTTP one.

    Only the HTTP-transport entry is pruned — a user-scope ``nauro`` defined as
    a stdio command is the user's own choice and is left alone. Soft-fails
    (never raises) so a malformed or absent file cannot break wiring. Returns a
    status line when something was removed, otherwise ``None``.
    """
    config_path = Path.home() / ".claude.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get("nauro")
    if not isinstance(entry, dict):
        return None
    if entry.get("type") != "http" and "url" not in entry:
        return None
    del servers["nauro"]
    if not servers:
        config.pop("mcpServers", None)
    try:
        config_path.write_text(json.dumps(config, indent=2) + "\n")
    except OSError:
        return None
    return (
        "  removed redundant user-scope HTTP nauro entry from ~/.claude.json "
        "(project-scope stdio is canonical)"
    )


@setup_app.command(name="claude-code")
def claude_code(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's advisory UserPromptSubmit hook into each repo's "
            "project-scope .claude/settings.json. The hook surfaces related "
            "decisions as context each turn and never blocks."
        ),
    ),
) -> None:
    """Configure Claude Code to use Nauro during sessions."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)

    # Clean up legacy CLAUDE.md blocks (behavioral guidance now delivered
    # via MCP server instructions, so the injected block is no longer needed).
    legacy_results = []
    mcp_results = []
    hook_results = []
    for repo_str in entry["repo_paths"]:
        repo_path = Path(repo_str)
        if not repo_path.is_dir():
            mcp_results.append(f"  {repo_path}: repo path missing, skipped")
            continue
        legacy = _remove_claude_md(repo_path)
        if legacy:
            legacy_results.append(legacy)
        mcp_results.append(_configure_mcp(repo_path, remove=remove))
        if with_hooks:
            try:
                hook_results.append(materialize_hooks_claude_code(repo_path, remove=remove))
            except Exception as exc:
                hook_results.append(f"  {repo_path}: hook wiring error — {exc}")

    if not remove:
        pruned = _prune_redundant_user_scope_mcp()
        if pruned:
            mcp_results.append(pruned)

    # Print summary
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}':\n")
    for line in mcp_results:
        typer.echo(line)

    if hook_results:
        typer.echo("\nHooks:")
        for line in hook_results:
            typer.echo(line)

    if legacy_results:
        typer.echo("\nLegacy cleanup:")
        for line in legacy_results:
            typer.echo(line)

    if not remove:
        # Regenerate AGENTS.md so context is fresh from the start. The store
        # dir name is the v2 id (or v1 name) used by the registry-aware lookup.
        updated_repos = regenerate_agents_md_for_project(_store_path.name, _store_path)
        if updated_repos:
            typer.echo("\nAGENTS.md:")
            for repo_path in updated_repos:
                typer.echo(f"  {repo_path}: regenerated AGENTS.md")

        typer.echo(
            "\nNext: start a Claude Code session in one of the repos."
            " The MCP server will start automatically."
        )
        if with_hooks:
            typer.echo(f"\n{HOOKS_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Cursor ─────────────────────────────────────────────────────────────────


# Cursor reads MCP servers from `<repo>/.cursor/mcp.json` (per-project).
# User-global "Rules for AI" live in the IDE Settings UI, not a file path —
# so MCP wiring is per-project here.
# Docs: https://cursor.com/docs


def _configure_cursor_for_repo(repo_path: Path, *, remove: bool) -> str:
    """Add or remove the Nauro MCP entry in this repo's ``.cursor/mcp.json``."""
    return _configure_json_mcp(
        repo_path,
        config_rel_path=".cursor/mcp.json",
        label=".cursor/mcp.json",
        remove=remove,
    )


@setup_app.command(name="cursor")
def cursor(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
) -> None:
    """Configure Cursor to use Nauro for this project's repos."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)

    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro (Cursor) for project '{project_name}':\n")
    for repo_str in entry["repo_paths"]:
        repo_path = Path(repo_str)
        if not repo_path.is_dir():
            typer.echo(f"  {repo_path}: repo path missing, skipped")
            continue
        typer.echo(_configure_cursor_for_repo(repo_path, remove=remove))

    if not remove:
        typer.echo("\nNext: open this repo in Cursor and start a chat — Nauro MCP will connect.")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Codex CLI ──────────────────────────────────────────────────────────────


# Codex reads MCP servers from `~/.codex/config.toml` under `[mcp_servers.<name>]`.
# This is the user-global Codex CLI config and is shared with the IDE extension.
# Docs: https://developers.openai.com/codex/mcp


def _default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _configure_codex(
    *,
    remove: bool,
    config_path: Path | None = None,
    clear_user_scope: bool = True,
) -> str:
    """Add or remove the Nauro MCP entry in ``~/.codex/config.toml``.

    ``clear_user_scope`` gates the remove path: when False, the codex MCP
    entry is preserved because other registered nauro projects still depend
    on it. Defaults to True so direct unit callers and the add path retain
    their previous behavior.
    """
    config_path = config_path or _default_codex_config_path()
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if remove and not clear_user_scope:
        return (
            f"Codex: preserved nauro entry in {config_path} (other nauro projects still registered)"
        )

    if config_path.exists():
        try:
            with config_path.open("rb") as f:
                config = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            return f"Codex: could not parse {config_path} — {exc}"
    else:
        config = {}

    servers = config.setdefault("mcp_servers", {})
    # A hand-edited config.toml could define mcp_servers as a non-table (e.g. a
    # string); mutating it would raise. Skip with a clear message, not a crash.
    if not isinstance(servers, dict):
        if remove:
            return f"Codex: no nauro entry to remove in {config_path}"
        return f"Codex: mcp_servers in {config_path} is not a table, skipped"

    if remove:
        if "nauro" in servers:
            del servers["nauro"]
            if not servers:
                config.pop("mcp_servers", None)
            config_path.write_bytes(tomli_w.dumps(config).encode("utf-8"))
            return f"Codex: removed nauro from {config_path}"
        return f"Codex: no nauro entry to remove in {config_path}"

    servers["nauro"] = nauro_entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(tomli_w.dumps(config).encode("utf-8"))
    return f"Codex: wrote nauro to {config_path}"


@setup_app.command(name="codex")
def codex(
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
) -> None:
    """Configure Codex CLI to use Nauro (writes ``~/.codex/config.toml``)."""
    # Standalone codex wiring is user-global, so removing it without first
    # tearing down the other projects would leave them pointed at a missing
    # MCP entry. Gate on the registry: only the last project's teardown clears.
    clear_user_scope = _user_scope_safe_to_clear(None) if remove else True
    typer.echo(_configure_codex(remove=remove, clear_user_scope=clear_user_scope))
    if not remove:
        typer.echo("\nNext: run a Codex session — it reads ~/.codex/config.toml on start.")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Skill materialization ──────────────────────────────────────────────────


# Skills are rendered from canonical bodies in nauro.skills and written into
# the user's surface directories. Claude Code and Codex skills are user-global;
# Cursor skills ship per-project (Cursor's "User Rules" live in the IDE
# Settings UI, not a file path).
#
# ``SKILL_NAMES`` is the always-installed set — the core onboarding skill.
# ``OPT_IN_SKILL_NAMES`` is materialized only when the caller passes
# ``with_skills=True``. ``nauro-ship-task`` references the bundled ``@nauro-*``
# subagents and is opt-in for that reason, so the ``--with-subagents`` notice
# stays scoped to it. ``nauro-handoff`` and ``nauro-context`` compose only
# existing MCP tools (plus the agent's own filesystem write) with no subagent
# dependency, so they carry no such notice.

SKILL_NAMES: tuple[str, ...] = ("nauro-adopt",)
OPT_IN_SKILL_NAMES: tuple[str, ...] = ("nauro-ship-task", "nauro-handoff", "nauro-context")


def _claude_skill_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def _codex_skill_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def _claude_agent_dir() -> Path:
    return Path.home() / ".claude" / "agents"


def _materialize_skill_file(target: Path, content: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"  wrote {target}"


def _remove_skill_file(target: Path, *, stop_above: Path) -> str:
    """Unlink ``target`` and prune empty parents, but never above ``stop_above``.

    Without the bound the parent walk could rmdir the surface base
    (``~/.claude/skills/``, ``<repo>/.cursor/rules/``, etc.) or — worse —
    keep going if those happen to be empty after MCP config also got removed.
    """
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
    with ``OPT_IN_SKILL_NAMES`` (``nauro-ship-task``, ``nauro-handoff``, and
    ``nauro-context``).
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
    with ``OPT_IN_SKILL_NAMES`` (``nauro-ship-task``, ``nauro-handoff``, and
    ``nauro-context``).
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
        target = base / f"{name}.mdc"
        if remove:
            results.append(_remove_skill_file(target, stop_above=base))
        else:
            results.append(_materialize_skill_file(target, render_skill("cursor", name)))
    return results


# ─── Agent materialization ──────────────────────────────────────────────────


# Subagents are rendered from canonical bodies in nauro.agents and written into
# the user's surface directories. On Claude Code, that's ``~/.claude/agents/``.
# Unlike skills, agents are namespaced (``nauro-*``) and opt-in. The
# ``nauro-`` namespace is bundle-owned (D177): on install, the current bundle
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
        bundled = render_agent("claude_code", name)
        if remove:
            if not target.is_file():
                results.append(f"  no agent at {target}")
                continue
            current = target.read_text(encoding="utf-8")
            if current == bundled:
                target.unlink()
                results.append(f"  removed {target}")
            else:
                results.append(f"  preserved {target} (locally modified)")
            continue

        if target.is_file():
            current = target.read_text(encoding="utf-8")
            if current == bundled:
                results.append(f"  unchanged {target}")
            elif force_overwrite:
                target.write_text(bundled, encoding="utf-8")
                results.append(f"  overwrote {target}")
            else:
                backup = target.parent / (target.name + ".bak")
                backup.write_text(current, encoding="utf-8")
                target.write_text(bundled, encoding="utf-8")
                results.append(f"  updated {target} (previous saved to {backup.name})")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(bundled, encoding="utf-8")
            results.append(f"  installed {target}")
    return results


# ─── Hook materialization ─────────────────────────────────────────────────────


# Claude Code reads hooks from project-scope ``<repo>/.claude/settings.json``.
# The advisory UserPromptSubmit hook runs ``nauro hook user-prompt-submit`` on
# each turn; it surfaces related decisions as context and never blocks a turn.
# The MVP hook is BM25-floor only — it does not set ``NAURO_EMBEDDINGS`` — so the
# install incurs no embedding model load. The hook still resolves the embeddings
# flag internally, so the follow-up that re-admits cosine-gated embedding hits
# can flip the backend on without changing the installed command.
#
# The hook is Claude-Code-only: Cursor and Codex have no comparable per-turn
# client event to bind to.

HOOK_EVENT_NAME = "UserPromptSubmit"
# The subcommand the hook entry runs; the full command is built at install time
# by prefixing the resolved absolute nauro path (see _nauro_hook_entry), so the
# hook fires even when nauro is not on the agent's launch PATH.
HOOK_SUBCOMMAND = "hook user-prompt-submit"
HOOK_TIMEOUT_SECONDS = 10

# Substring that identifies a nauro-authored hook entry on the remove path, so a
# user's own UserPromptSubmit hooks are preserved. Matches the subcommand rather
# than "nauro " so it holds regardless of how the entrypoint resolves — a bare
# "nauro", an absolute POSIX path, or a Windows "nauro.exe".
_HOOK_COMMAND_MARKER = HOOK_SUBCOMMAND


def _claude_settings_path(repo: Path) -> Path:
    return repo / ".claude" / "settings.json"


def materialize_hooks_claude_code(repo: Path, *, remove: bool) -> str:
    """Add or remove the Nauro advisory hook in ``<repo>/.claude/settings.json``.

    Add path: idempotently append the hook entry to
    ``hooks.UserPromptSubmit[].hooks[]`` only when no nauro-authored entry is
    already present. Remove path: strip only the nauro-authored entry (matched on
    the command containing ``nauro hook``), preserving any user-authored hooks
    and the surrounding structure.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    settings_path = _claude_settings_path(repo)

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError as exc:
            return f"  {repo}: could not parse .claude/settings.json — {exc}"
        if not isinstance(settings, dict):
            return f"  {repo}: .claude/settings.json is not a JSON object, skipped"
    else:
        settings = {}

    if remove:
        return _remove_hook_entry(settings_path, settings, repo)
    return _add_hook_entry(settings_path, settings, repo)


def _nauro_hook_entry() -> dict:
    return {
        "type": "command",
        "command": f"{_find_nauro_command()} {HOOK_SUBCOMMAND}",
        "timeout": HOOK_TIMEOUT_SECONDS,
    }


def _is_nauro_hook(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
        and _HOOK_COMMAND_MARKER in entry["command"]
    )


def _add_hook_entry(settings_path: Path, settings: dict, repo: Path) -> str:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return f"  {repo}: hooks key is not a JSON object, skipped"
    event_matchers = hooks.setdefault(HOOK_EVENT_NAME, [])
    if not isinstance(event_matchers, list):
        return f"  {repo}: hooks.{HOOK_EVENT_NAME} is not a JSON array, skipped"

    # Idempotent: if any matcher already carries a nauro hook, do nothing.
    for matcher in event_matchers:
        if isinstance(matcher, dict):
            for entry in matcher.get("hooks", []):
                if _is_nauro_hook(entry):
                    return f"  {repo}: nauro hook already present in .claude/settings.json"

    event_matchers.append({"hooks": [_nauro_hook_entry()]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return f"  {repo}: wrote nauro hook to .claude/settings.json"


def _remove_hook_entry(settings_path: Path, settings: dict, repo: Path) -> str:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return f"  {repo}: no nauro hook to remove"
    event_matchers = hooks.get(HOOK_EVENT_NAME)
    if not isinstance(event_matchers, list):
        return f"  {repo}: no nauro hook to remove"

    removed = False
    surviving_matchers = []
    for matcher in event_matchers:
        if not isinstance(matcher, dict):
            surviving_matchers.append(matcher)
            continue
        entries = matcher.get("hooks", [])
        kept = [e for e in entries if not _is_nauro_hook(e)]
        if len(kept) != len(entries):
            removed = True
        if kept:
            matcher = {**matcher, "hooks": kept}
            surviving_matchers.append(matcher)
        elif "hooks" not in matcher:
            surviving_matchers.append(matcher)
        # A matcher whose only hooks were nauro-authored is dropped entirely.

    if not removed:
        return f"  {repo}: no nauro hook to remove"

    if surviving_matchers:
        hooks[HOOK_EVENT_NAME] = surviving_matchers
    else:
        hooks.pop(HOOK_EVENT_NAME, None)
    if not hooks:
        settings.pop("hooks", None)

    if settings:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    else:
        settings_path.unlink()
    return f"  {repo}: removed nauro hook from .claude/settings.json"


# ─── nauro setup all ────────────────────────────────────────────────────────


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
) -> list[str]:
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
    (``nauro-ship-task``, ``nauro-handoff``, ``nauro-context``). Independent of
    ``with_subagents`` so users
    can adopt skills and subagents on separate cadences, though
    ``nauro-ship-task`` references the bundled ``@nauro-*`` subagents in
    its body — caller surfaces ``with_skills`` without ``with_subagents``
    should warn the user.

    ``with_hooks`` opts into wiring the advisory ``UserPromptSubmit`` hook into
    each repo's project-scope ``.claude/settings.json`` (Claude-Code-only).
    Off by default. A hook-wiring failure is caught and reported as a status
    line so it never aborts the rest of setup.
    """
    clear_user_scope = _user_scope_safe_to_clear(current_project_key) if remove else True

    lines: list[str] = []

    # Claude Code (MCP per-repo via direct `.mcp.json` write + skills global)
    for repo in project_repos:
        if not repo.is_dir():
            lines.append(f"  {repo}: repo path missing, skipped")
            continue
        try:
            lines.append(_configure_mcp(repo, remove=remove))
        except Exception as exc:
            lines.append(f"Claude Code MCP ({repo}): error — {exc}")
    if not remove:
        try:
            pruned = _prune_redundant_user_scope_mcp()
            if pruned:
                lines.append(pruned)
        except Exception as exc:  # never let cleanup break wiring
            lines.append(f"Claude Code MCP (user-scope cleanup): error — {exc}")
    try:
        lines.extend(
            materialize_skills_claude_code(
                remove=remove,
                clear_user_scope=clear_user_scope,
                with_skills=with_skills,
            )
        )
    except Exception as exc:
        lines.append(f"Claude Code skills: error — {exc}")

    if with_subagents:
        try:
            lines.extend(
                materialize_agents(
                    "claude_code",
                    remove=remove,
                    force_overwrite=force_overwrite,
                    clear_user_scope=clear_user_scope,
                )
            )
        except Exception as exc:
            lines.append(f"Claude Code agents: error — {exc}")

    if with_hooks:
        for repo in project_repos:
            if not repo.is_dir():
                continue
            try:
                lines.append(materialize_hooks_claude_code(repo, remove=remove))
            except Exception as exc:
                lines.append(f"Claude Code hook ({repo}): error — {exc}")

    # Cursor (MCP per-repo + skills per-repo)
    for repo in project_repos:
        if not repo.is_dir():
            continue
        try:
            lines.append(_configure_cursor_for_repo(repo, remove=remove))
        except Exception as exc:
            lines.append(f"Cursor MCP ({repo}): error — {exc}")
        try:
            lines.extend(
                materialize_skills_cursor_for_repo(repo, remove=remove, with_skills=with_skills)
            )
        except Exception as exc:
            lines.append(f"Cursor skills ({repo}): error — {exc}")

    # Codex (MCP global + skills global)
    try:
        lines.append(_configure_codex(remove=remove, clear_user_scope=clear_user_scope))
    except Exception as exc:
        lines.append(f"Codex MCP: error — {exc}")
    try:
        lines.extend(
            materialize_skills_codex(
                remove=remove,
                clear_user_scope=clear_user_scope,
                with_skills=with_skills,
            )
        )
    except Exception as exc:
        lines.append(f"Codex skills: error — {exc}")

    # Regenerate AGENTS.md once so context is fresh from the start on every
    # entry point that wires surfaces. Guarded on the add path and on having a
    # store to read from.
    if not remove and current_project_key is not None and store_path is not None:
        try:
            updated = regenerate_agents_md_for_project(current_project_key, store_path)
            for repo_path in updated:
                lines.append(f"  {repo_path}: regenerated AGENTS.md")
        except Exception as exc:
            lines.append(f"AGENTS.md regeneration: error — {exc}")

    return lines


SHIP_TASK_NEEDS_SUBAGENTS_NOTICE = (
    "nauro-ship-task references the bundled @nauro-* subagents; pass "
    "`--with-subagents` to install them too."
)

# The bundled subagents allow the cloud tools by the fixed name
# `mcp__claude_ai_Nauro__*`. That prefix only resolves when the remote
# connector is named exactly `Nauro`, so surface the requirement whenever
# subagents are installed.
SUBAGENTS_CONNECTOR_NAME_NOTICE = (
    "Cloud users: name the remote MCP connector exactly `Nauro` so the bundled "
    "@nauro-* subagents' `mcp__claude_ai_Nauro__*` tools resolve."
)

HOOKS_NOTICE = (
    "The advisory hook surfaces related decisions as context on each turn "
    "(BM25 retrieval) and never blocks. Start a new Claude Code session in a "
    "wired repo for it to take effect."
)

# Multi-surface restart handoff. MCP config is read at session start, so an
# already-open session won't see the new wiring until it restarts. The
# single-tool `setup claude-code` prints its own equivalent line.
ALL_RESTART_NOTICE = (
    "Next: start a fresh agent session (Claude Code/Cursor) — MCP config is read at session start."
)


@setup_app.command(name="all")
def all_(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
    with_subagents: bool = typer.Option(
        False,
        "--with-subagents",
        help=(
            "Install Nauro's bundled workflow subagents (@nauro-planner, "
            "@nauro-executor, @nauro-reviewer, @nauro-tech-lead) into "
            "~/.claude/agents/. Off by default to avoid overwriting "
            "customized files."
        ),
    ),
    force_overwrite: bool = typer.Option(
        False,
        "--force-overwrite",
        help=(
            "Overwrite ~/.claude/agents/nauro-*.md in place without saving a "
            ".bak, when --with-subagents is passed. By default, install "
            "refreshes a differing bundled file and stashes its prior content "
            "to <name>.md.bak."
        ),
    ),
    with_skills: bool = typer.Option(
        False,
        "--with-skills",
        help=(
            "Install Nauro's bundled opt-in skills "
            "(/nauro-ship-task, /nauro-handoff, /nauro-context) alongside the "
            "always-installed /nauro-adopt skill. Independent of --with-subagents."
        ),
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's advisory UserPromptSubmit hook into each repo's "
            "project-scope .claude/settings.json (Claude Code only). The hook "
            "surfaces related decisions as context each turn and never blocks."
        ),
    ),
) -> None:
    """Configure Claude Code, Cursor, and Codex CLI in one call."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)

    project_repos = [Path(rp) for rp in entry["repo_paths"]]
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}' across all surfaces:\n")
    for line in setup_all_surfaces(
        project_repos,
        remove=remove,
        current_project_key=_store_path.name,
        store_path=_store_path,
        with_subagents=with_subagents,
        force_overwrite=force_overwrite,
        with_skills=with_skills,
        with_hooks=with_hooks,
    ):
        typer.echo(line)

    if not remove and with_skills and not with_subagents:
        typer.echo(f"\n{SHIP_TASK_NEEDS_SUBAGENTS_NOTICE}")

    if not remove and with_subagents:
        typer.echo(f"\n{SUBAGENTS_CONNECTOR_NAME_NOTICE}")

    if not remove and with_hooks:
        typer.echo(f"\n{HOOKS_NOTICE}")

    if not remove:
        typer.echo(f"\n{ALL_RESTART_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")
