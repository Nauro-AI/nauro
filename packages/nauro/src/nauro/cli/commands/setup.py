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
import subprocess
import sys
from pathlib import Path

import typer

from nauro.cli.utils import resolve_target_project
from nauro.constants import CLAUDE_MD, NAURO_BLOCK_END, NAURO_BLOCK_START
from nauro.store.registry import (
    RegistrySchemaError,
    get_project,
    get_project_v2,
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
    """Find the full path to the nauro binary for the MCP config."""
    path = shutil.which("nauro")
    return path if path else "nauro"


# claude mcp remove emits these on a missing entry — treat as graceful no-op.
_CLAUDE_REMOVE_NOT_FOUND_MARKERS = ("no mcp server", "not found", "does not exist")


def _configure_mcp(repo_path: Path, *, remove: bool = False) -> str:
    """Add or remove the Nauro MCP entry in Claude Code via the ``claude`` CLI.

    Uses ``--scope project`` so the entry is written to ``<repo>/.mcp.json``,
    making it shareable with collaborators via git — mirroring the Cursor
    surface model. The shell-out is intentional: Claude Code's per-project
    config layout has changed before; ``claude mcp add`` is the supported
    entry point.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    if shutil.which("claude") is None:
        if remove:
            return "Claude Code CLI not found on PATH; nothing to remove."
        return (
            "Claude Code CLI not found on PATH; skipping Claude Code wiring "
            "(run 'nauro setup claude-code' after installing it)."
        )

    nauro_cmd = _find_nauro_command()

    if remove:
        result = subprocess.run(
            ["claude", "mcp", "remove", "nauro"],
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return f"  {repo_path}: removed nauro from .mcp.json"
        stderr_lower = (result.stderr or "").lower()
        if any(marker in stderr_lower for marker in _CLAUDE_REMOVE_NOT_FOUND_MARKERS):
            return f"  {repo_path}: no nauro entry to remove"
        return f"  {repo_path}: claude mcp remove failed — {(result.stderr or '').strip()}"

    result = subprocess.run(
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "project",
            "nauro",
            "--",
            nauro_cmd,
            "serve",
            "--stdio",
        ],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return f"  {repo_path}: wrote nauro to .mcp.json"
    return f"  {repo_path}: claude mcp add failed — {(result.stderr or '').strip()}"


@setup_app.command(name="claude-code")
def claude_code(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
) -> None:
    """Configure Claude Code to use Nauro during sessions."""
    project_name, _store_path = resolve_target_project(project)
    project_key = _store_path.name

    # Try v2 first (id-keyed), then fall back to v1 (name-keyed legacy)
    try:
        entry = get_project_v2(project_key)
    except RegistrySchemaError:
        entry = None
    if entry is None:
        entry = get_project(project_name)

    if entry is None or not entry.get("repo_paths"):
        typer.echo(f"Project '{project_name}' has no associated repos.", err=True)
        raise typer.Exit(code=1)

    # Clean up legacy CLAUDE.md blocks (behavioral guidance now delivered
    # via MCP server instructions, so the injected block is no longer needed).
    legacy_results = []
    mcp_results = []
    for repo_str in entry["repo_paths"]:
        repo_path = Path(repo_str)
        if not repo_path.is_dir():
            mcp_results.append(f"  {repo_path}: repo path missing, skipped")
            continue
        legacy = _remove_claude_md(repo_path)
        if legacy:
            legacy_results.append(legacy)
        mcp_results.append(_configure_mcp(repo_path, remove=remove))

    # Print summary
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}':\n")
    for line in mcp_results:
        typer.echo(line)

    if legacy_results:
        typer.echo("\nLegacy cleanup:")
        for line in legacy_results:
            typer.echo(line)

    if not remove:
        # Regenerate AGENTS.md so context is fresh from the start.
        # project_key is the v2 id (or v1 name) used by the registry-aware lookup.
        updated_repos = regenerate_agents_md_for_project(project_key, _store_path)
        if updated_repos:
            typer.echo("\nAGENTS.md:")
            for repo_path in updated_repos:
                typer.echo(f"  {repo_path}: regenerated AGENTS.md")

        typer.echo(
            "\nNext: start a Claude Code session in one of the repos."
            " The MCP server will start automatically."
        )


# ─── Cursor ─────────────────────────────────────────────────────────────────


# Cursor reads MCP servers from `<repo>/.cursor/mcp.json` (per-project).
# User-global "Rules for AI" live in the IDE Settings UI, not a file path —
# so MCP wiring is per-project here.
# Docs: https://cursor.com/docs (checked: 2026-05-07)


def _configure_cursor_for_repo(repo_path: Path, *, remove: bool) -> str:
    """Add or remove the Nauro MCP entry in this repo's ``.cursor/mcp.json``."""
    cursor_dir = repo_path / ".cursor"
    config_path = cursor_dir / "mcp.json"
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if config_path.exists():
        config = json.loads(config_path.read_text())
    else:
        config = {}

    if remove:
        servers = config.get("mcpServers", {})
        if "nauro" in servers:
            del servers["nauro"]
            if not servers:
                config.pop("mcpServers", None)
            if config:
                config_path.write_text(json.dumps(config, indent=2) + "\n")
            else:
                config_path.unlink()
            return f"  {repo_path}: removed nauro from .cursor/mcp.json"
        return f"  {repo_path}: no nauro entry to remove"

    config.setdefault("mcpServers", {})["nauro"] = nauro_entry
    cursor_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return f"  {repo_path}: wrote nauro to .cursor/mcp.json"


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
    project_key = _store_path.name

    try:
        entry = get_project_v2(project_key)
    except RegistrySchemaError:
        entry = None
    if entry is None:
        entry = get_project(project_name)

    if entry is None or not entry.get("repo_paths"):
        typer.echo(f"Project '{project_name}' has no associated repos.", err=True)
        raise typer.Exit(code=1)

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


# ─── Codex CLI ──────────────────────────────────────────────────────────────


# Codex reads MCP servers from `~/.codex/config.toml` under `[mcp_servers.<name>]`.
# This is the user-global Codex CLI config and is shared with the IDE extension.
# Docs: https://developers.openai.com/codex/mcp (checked: 2026-05-07)


def _default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _configure_codex(*, remove: bool, config_path: Path | None = None) -> str:
    """Add or remove the Nauro MCP entry in ``~/.codex/config.toml``."""
    config_path = config_path or _default_codex_config_path()
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if config_path.exists():
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    else:
        config = {}

    servers = config.setdefault("mcp_servers", {})

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
    typer.echo(_configure_codex(remove=remove))
    if not remove:
        typer.echo("\nNext: run a Codex session — it reads ~/.codex/config.toml on start.")


# ─── Skill materialization ──────────────────────────────────────────────────


# Skills are rendered from canonical bodies in nauro.skills and written into
# the user's surface directories. Claude Code and Codex skills are user-global;
# Cursor skills ship per-project (Cursor's "User Rules" live in the IDE
# Settings UI, not a file path).

SKILL_NAMES: tuple[str, ...] = ("nauro", "nauro-adopt")


def _claude_skill_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def _codex_skill_dir() -> Path:
    return Path.home() / ".agents" / "skills"


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


def materialize_skills_claude_code(*, remove: bool) -> list[str]:
    """Install or remove the two Nauro skills under ``~/.claude/skills/``."""
    from nauro.skills import render_skill

    base = _claude_skill_dir()
    results: list[str] = []
    for name in SKILL_NAMES:
        target = base / name / "SKILL.md"
        if remove:
            results.append(_remove_skill_file(target, stop_above=base))
        else:
            results.append(_materialize_skill_file(target, render_skill("claude_code", name)))
    return results


def materialize_skills_codex(*, remove: bool) -> list[str]:
    """Install or remove the two Nauro skills under ``~/.agents/skills/``."""
    from nauro.skills import render_skill

    base = _codex_skill_dir()
    results: list[str] = []
    for name in SKILL_NAMES:
        target = base / name / "SKILL.md"
        if remove:
            results.append(_remove_skill_file(target, stop_above=base))
        else:
            results.append(_materialize_skill_file(target, render_skill("codex", name)))
    return results


def materialize_skills_cursor_for_repo(repo: Path, *, remove: bool) -> list[str]:
    """Install or remove Cursor rules under ``<repo>/.cursor/rules/``."""
    from nauro.skills import render_skill

    base = repo / ".cursor" / "rules"
    results: list[str] = []
    for name in SKILL_NAMES:
        target = base / f"{name}.mdc"
        if remove:
            results.append(_remove_skill_file(target, stop_above=base))
        else:
            results.append(_materialize_skill_file(target, render_skill("cursor", name)))
    return results


# ─── nauro setup all ────────────────────────────────────────────────────────


def setup_all_surfaces(project_repos: list[Path], *, remove: bool = False) -> list[str]:
    """Wire MCP and materialize skills across Claude Code, Cursor, Codex.

    Continues across per-handler errors so partial coverage still reports
    progress. Returns the cumulative status lines.
    """
    lines: list[str] = []

    # Claude Code (MCP per-repo via `claude mcp add --scope project` + skills global)
    for repo in project_repos:
        if not repo.is_dir():
            lines.append(f"  {repo}: repo path missing, skipped")
            continue
        try:
            lines.append(_configure_mcp(repo, remove=remove))
        except Exception as exc:
            lines.append(f"Claude Code MCP ({repo}): error — {exc}")
    try:
        lines.extend(materialize_skills_claude_code(remove=remove))
    except Exception as exc:
        lines.append(f"Claude Code skills: error — {exc}")

    # Cursor (MCP per-repo + skills per-repo)
    for repo in project_repos:
        if not repo.is_dir():
            continue
        try:
            lines.append(_configure_cursor_for_repo(repo, remove=remove))
        except Exception as exc:
            lines.append(f"Cursor MCP ({repo}): error — {exc}")
        try:
            lines.extend(materialize_skills_cursor_for_repo(repo, remove=remove))
        except Exception as exc:
            lines.append(f"Cursor skills ({repo}): error — {exc}")

    # Codex (MCP global + skills global)
    try:
        lines.append(_configure_codex(remove=remove))
    except Exception as exc:
        lines.append(f"Codex MCP: error — {exc}")
    try:
        lines.extend(materialize_skills_codex(remove=remove))
    except Exception as exc:
        lines.append(f"Codex skills: error — {exc}")

    return lines


@setup_app.command(name="all")
def all_(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
) -> None:
    """Configure Claude Code, Cursor, and Codex CLI in one call."""
    project_name, _store_path = resolve_target_project(project)
    project_key = _store_path.name

    try:
        entry = get_project_v2(project_key)
    except RegistrySchemaError:
        entry = None
    if entry is None:
        entry = get_project(project_name)

    if entry is None or not entry.get("repo_paths"):
        typer.echo(f"Project '{project_name}' has no associated repos.", err=True)
        raise typer.Exit(code=1)

    project_repos = [Path(rp) for rp in entry["repo_paths"]]
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}' across all surfaces:\n")
    for line in setup_all_surfaces(project_repos, remove=remove):
        typer.echo(line)
