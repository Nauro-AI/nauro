"""JSON MCP config codec (.mcp.json and .cursor/mcp.json) for the setup surface."""

from __future__ import annotations

import json
from pathlib import Path

from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.cli.nauro_command import _find_nauro_command
from nauro.store._atomic import atomic_write_text
from nauro.store.write_safety import find_symlink


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
    refusal = find_symlink(repo_path, config_rel_path)
    if refusal is not None:
        return f"  {repo_path}: {refusal.message}"
    config_path = repo_path / config_rel_path
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"  {repo_path}: could not parse {label} - {exc}"
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
            atomic_write_text(config_path, json.dumps(config, indent=2) + "\n")
        else:
            config_path.unlink()
        return f"  {repo_path}: removed nauro from {label}"

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return f"  {repo_path}: mcpServers in {label} is not a JSON object, skipped"
    servers["nauro"] = nauro_entry
    atomic_write_text(config_path, json.dumps(config, indent=2) + "\n")
    lines = [f"  {repo_path}: wrote nauro to {label}"]
    lines.extend(public_surface_git_warnings(repo_path, config_rel_path))
    return "\n".join(lines)


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


def _configure_cursor_for_repo(repo_path: Path, *, remove: bool) -> str:
    """Add or remove the Nauro MCP entry in this repo's ``.cursor/mcp.json``."""
    return _configure_json_mcp(
        repo_path,
        config_rel_path=".cursor/mcp.json",
        label=".cursor/mcp.json",
        remove=remove,
    )
