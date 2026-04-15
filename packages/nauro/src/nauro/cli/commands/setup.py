"""nauro setup — Configure tool integrations.

Currently supports:
  nauro setup claude-code  — register MCP server + regenerate AGENTS.md
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from nauro.cli.utils import resolve_target_project
from nauro.constants import CLAUDE_MD, NAURO_BLOCK_END, NAURO_BLOCK_START
from nauro.store.registry import get_project
from nauro.templates.agents_md import regenerate_agents_md_for_project

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


def _find_claude_config_path() -> Path | None:
    """Find the Claude Code MCP config file path."""
    candidates = [
        Path.home() / ".claude" / "claude_desktop_config.json",
        Path.home() / ".config" / "claude" / "claude_desktop_config.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # If neither exists, prefer the first path if its parent exists
    for candidate in candidates:
        if candidate.parent.exists():
            return candidate
    return None


def _find_nauro_command() -> str:
    """Find the full path to the nauro binary for the MCP config."""
    import shutil

    path = shutil.which("nauro")
    return path if path else "nauro"


def _configure_mcp(remove: bool = False) -> str:
    """Add or remove the Nauro MCP entry in Claude Code's config.

    Uses stdio transport — Claude Code spawns the server process directly.
    Returns a status string.
    """
    config_path = _find_claude_config_path()
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if config_path is None:
        if remove:
            return "MCP config: no Claude Code config found, nothing to remove"
        snippet = json.dumps({"mcpServers": {"nauro": nauro_entry}}, indent=2)
        return (
            "MCP config: could not find Claude Code config file.\n"
            "  Add the following to your Claude Code MCP config:\n"
            f"  {snippet}"
        )

    if config_path.exists():
        config = json.loads(config_path.read_text())
    else:
        config = {}

    if remove:
        servers = config.get("mcpServers", {})
        if "nauro" in servers:
            del servers["nauro"]
            if not servers:
                del config["mcpServers"]
            config_path.write_text(json.dumps(config, indent=2) + "\n")
            return f"MCP config: removed nauro from {config_path}"
        return f"MCP config: no nauro entry found in {config_path}"

    if "mcpServers" not in config:
        config["mcpServers"] = {}
    config["mcpServers"]["nauro"] = nauro_entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return f"MCP config: wrote nauro server to {config_path}"


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
    entry = get_project(project_name)

    if entry is None or not entry.get("repo_paths"):
        typer.echo(f"Project '{project_name}' has no associated repos.", err=True)
        raise typer.Exit(code=1)

    # Clean up legacy CLAUDE.md blocks (behavioral guidance now delivered
    # via MCP server instructions, so the injected block is no longer needed).
    legacy_results = []
    for repo_str in entry["repo_paths"]:
        repo_path = Path(repo_str)
        if not repo_path.is_dir():
            continue
        result = _remove_claude_md(repo_path)
        if result:
            legacy_results.append(result)

    mcp_result = _configure_mcp(remove=remove)

    # Print summary
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}':\n")
    typer.echo(mcp_result)

    if legacy_results:
        typer.echo("\nLegacy cleanup:")
        for line in legacy_results:
            typer.echo(line)

    if not remove:
        # Regenerate AGENTS.md so context is fresh from the start
        updated_repos = regenerate_agents_md_for_project(project_name, _store_path)
        if updated_repos:
            typer.echo("\nAGENTS.md:")
            for repo_path in updated_repos:
                typer.echo(f"  {repo_path}: regenerated AGENTS.md")

        typer.echo(
            "\nNext: start a Claude Code session in one of the repos."
            " The MCP server will start automatically."
        )
