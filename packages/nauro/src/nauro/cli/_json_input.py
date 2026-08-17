"""Internal CLI helper for parsing ``list[dict]`` arguments.

The auto-gen framework uses this to turn a single Typer flag into the structured
value the matching MCP adapter expects. Three input sources sit behind one flag:
literal JSON (``--rejected '[{"alternative": "X"}]'``), ``@path`` reading a JSON
file, and ``-`` reading stdin.

All parse failures raise ``typer.BadParameter``, which Typer renders to stderr at
exit 2 without invoking the adapter. That keeps the split clean: kernel-side
rejections flow through the JSON envelope on stdout at exit 0, CLI argument-parse
failures stay on stderr at exit 2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer


def parse_json_list_of_dicts(raw: str, flag_name: str) -> list[dict]:
    """Parse a flag value that accepts inline JSON, ``@file``, or ``-`` for stdin.
    Returns a ``list[dict]``; anything unreadable, non-JSON, or not a list of objects
    raises ``typer.BadParameter`` naming ``flag_name``.
    """
    if raw == "-":
        text = sys.stdin.read()
        if not text:
            raise typer.BadParameter(f"{flag_name}: stdin closed without input")
    elif raw.startswith("@"):
        path = Path(raw[1:])
        if not path.exists() or not path.is_file():
            raise typer.BadParameter(f"{flag_name}: file '{path}' does not exist")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise typer.BadParameter(f"{flag_name}: file '{path}' is not valid UTF-8") from exc
    else:
        text = raw

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{flag_name}: invalid JSON ({exc.msg})") from exc

    if not isinstance(parsed, list):
        raise typer.BadParameter(
            f"{flag_name}: expected JSON array of objects, got {type(parsed).__name__}"
        )
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise typer.BadParameter(f"{flag_name}: element [{idx}] is not an object")
    return parsed
