"""User-scope ~/.claude.json MCP prune for the setup surface."""

from __future__ import annotations

import json
from pathlib import Path

from nauro.cli.integrations._json_config import write_json_config
from nauro.cli.integrations.outcomes import ClaudeUserConfigKind, ClaudeUserConfigOutcome
from nauro.store.write_safety import find_file_symlink


def _prune_redundant_user_scope_mcp() -> ClaudeUserConfigOutcome | None:
    """Remove a redundant user-scope HTTP ``nauro`` entry from ``~/.claude.json``.
    Only the HTTP-transport entry is pruned; a user's own stdio entry is left alone, and the
    function soft-fails. Returns a status line when it acted or could not read, else ``None``.
    """
    config_path = Path.home() / ".claude.json"
    if not config_path.exists():
        return None
    refusal = find_file_symlink(config_path)
    if refusal is not None:
        return ClaudeUserConfigOutcome(ClaudeUserConfigKind.REFUSED_SYMLINK, refusal=refusal)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return ClaudeUserConfigOutcome(ClaudeUserConfigKind.INVALID_UTF8)
    except (json.JSONDecodeError, OSError):
        return None
    # A hand-mangled ~/.claude.json can parse to a non-object top level (an
    # explicit null, an array, or a scalar); ``.get`` would raise on it. Skip
    # gracefully, mirroring the shape guard the write codecs apply.
    if not isinstance(config, dict):
        return ClaudeUserConfigOutcome(ClaudeUserConfigKind.NOT_JSON_OBJECT)
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
        write_json_config(config_path, config)
    except OSError:
        return None
    return ClaudeUserConfigOutcome(ClaudeUserConfigKind.PRUNED)
