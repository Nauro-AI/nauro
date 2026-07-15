"""User-scope ~/.claude.json MCP prune for the setup surface."""

from __future__ import annotations

import json
from pathlib import Path

from nauro.store._atomic import atomic_write_text
from nauro.store.write_safety import find_file_symlink


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
    status line when something was removed or when the file is not valid
    UTF-8, otherwise ``None``.
    """
    config_path = Path.home() / ".claude.json"
    if not config_path.exists():
        return None
    refusal = find_file_symlink(config_path)
    if refusal is not None:
        return f"  skipped user-scope prune: {refusal.message}"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return "  skipped user-scope prune: ~/.claude.json is not valid UTF-8"
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
        atomic_write_text(config_path, json.dumps(config, indent=2) + "\n")
    except OSError:
        return None
    return (
        "  removed redundant user-scope HTTP nauro entry from ~/.claude.json "
        "(project-scope stdio is canonical)"
    )
