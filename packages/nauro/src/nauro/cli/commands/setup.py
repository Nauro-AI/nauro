"""nauro setup — Configure tool integrations.

Currently supports:
  nauro setup claude-code  — inject CLAUDE.md directives + MCP config
  nauro setup claude-code --hooks — install Claude Code compaction hooks
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

CLAUDE_MD_START = NAURO_BLOCK_START
CLAUDE_MD_END = NAURO_BLOCK_END


def _claude_md_block() -> str:
    """Generate the CLAUDE.md integration block."""
    return f"""{CLAUDE_MD_START}

## Nauro — project context

This project uses Nauro for persistent context. The MCP server is
auto-started by Claude Code. Project context has been injected at
session start via AGENTS.md.

---

## When to propose a decision

Call `propose_decision` when you choose between two or more approaches,
replace or remove a dependency, establish a new pattern, or cut
something from scope. Do not wait until the end of the session —
log decisions at the moment they are made. Use `check_decision` first
for advisory conflict checks, then `confirm_decision` after reviewing
validation results.

Examples that warrant a decision:
- Choosing FastAPI over Flask for the MCP server
- Deciding to defer multi-repo sync to v2
- Establishing that all store writes go through writer.py, never direct
- Removing Redis as a dependency by switching to procrastinate

Examples that do not:
- Fixing a bug with an obvious solution
- Adding a test for existing behavior
- Renaming a variable

{CLAUDE_MD_END}"""


def _inject_claude_md(repo_path: Path, project_name: str) -> str:
    """Inject or replace the Nauro block in CLAUDE.md.

    Returns a status string describing what happened.
    """
    claude_md = repo_path / CLAUDE_MD
    block = _claude_md_block()

    if claude_md.exists():
        content = claude_md.read_text()
        if CLAUDE_MD_START in content and CLAUDE_MD_END in content:
            # Replace existing block
            before = content[: content.index(CLAUDE_MD_START)]
            after = content[content.index(CLAUDE_MD_END) + len(CLAUDE_MD_END) :]
            new_content = before + block + after
            claude_md.write_text(new_content)
            return f"  {repo_path}: updated existing Nauro block in {CLAUDE_MD}"
        else:
            # Append block
            if not content.endswith("\n"):
                content += "\n"
            claude_md.write_text(content + "\n" + block + "\n")
            return f"  {repo_path}: appended Nauro block to {CLAUDE_MD}"
    else:
        claude_md.write_text(block + "\n")
        return f"  {repo_path}: created {CLAUDE_MD}"


def _remove_claude_md(repo_path: Path) -> str:
    """Remove the Nauro block from CLAUDE.md.

    Returns a status string describing what happened.
    """
    claude_md = repo_path / CLAUDE_MD
    if not claude_md.exists():
        return f"  {repo_path}: no {CLAUDE_MD} found"

    content = claude_md.read_text()
    if CLAUDE_MD_START not in content:
        return f"  {repo_path}: no Nauro block found in {CLAUDE_MD}"

    before = content[: content.index(CLAUDE_MD_START)]
    after = content[content.index(CLAUDE_MD_END) + len(CLAUDE_MD_END) :]
    remaining = (before + after).strip()

    if not remaining:
        claude_md.unlink()
        return f"  {repo_path}: deleted {CLAUDE_MD} (only contained Nauro block)"
    else:
        claude_md.write_text(remaining + "\n")
        return f"  {repo_path}: removed Nauro block from {CLAUDE_MD}"


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

    repo_results = []
    for repo_str in entry["repo_paths"]:
        repo_path = Path(repo_str)
        if not repo_path.is_dir():
            repo_results.append(f"  {repo_path}: directory not found, skipped")
            continue
        if remove:
            repo_results.append(_remove_claude_md(repo_path))
        else:
            repo_results.append(_inject_claude_md(repo_path, project_name))

    mcp_result = _configure_mcp(remove=remove)

    # Print summary
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}':\n")
    typer.echo("Repos:")
    for line in repo_results:
        typer.echo(line)
    typer.echo(f"\n{mcp_result}")

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
