"""Shared atomic writer for the JSON config sinks in ``nauro setup``.

``.mcp.json``, ``.cursor/mcp.json``, ``.claude/settings.json``, and the
user-scope ``~/.claude.json`` prune all serialize the same way: two-space
indent with a trailing newline, written atomically. One writer keeps that
serialization in a single place. Codex config (tomlkit, ``newline="\\n"``)
and Codex hooks (``_format_codex_hooks``) have their own serializers and are
deliberately not routed through here.
"""

from __future__ import annotations

import json
from pathlib import Path

from nauro.store._atomic import atomic_write_text


def write_json_config(path: Path, config: object) -> None:
    """Serialize ``config`` as indented JSON and write it atomically."""
    atomic_write_text(path, json.dumps(config, indent=2) + "\n")
