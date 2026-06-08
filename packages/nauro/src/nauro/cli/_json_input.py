"""Internal CLI helper for parsing ``list[dict]`` arguments.

The auto-gen framework uses this to translate a single Typer flag into
the structured value the matching MCP adapter expects. Three input
sources are supported behind one flag:

- literal JSON on the command line:
  ``--rejected '[{"alternative": "X", "reason": "Y"}]'``
- ``@path`` sigil reading a JSON file:
  ``--rejected @rejected.json``
- ``-`` sigil reading stdin:
  ``echo '[...]' | nauro propose-decision ... --rejected -``

All parse failures raise ``typer.BadParameter``. Typer renders that to
stderr with exit code 2; the adapter is never invoked. This keeps the
semantic split clean: kernel-side rejections flow through the JSON
envelope on stdout at exit 0, CLI argument-parse failures stay on
stderr at exit 2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer


def parse_json_list_of_dicts(raw: str, flag_name: str) -> list[dict]:
    """Parse a CLI argument that accepts inline JSON, ``@file``, or stdin.

    Args:
        raw: The literal value passed to the flag.
        flag_name: The long-form flag name (e.g. ``"--rejected"``) used
            verbatim in the error message so the user sees which flag
            failed.

    Returns:
        The parsed JSON value, guaranteed to be a ``list[dict]``.

    Raises:
        typer.BadParameter: When the value cannot be read, is not valid
            JSON, or is not a list of objects.
    """
    if raw == "-":
        text = sys.stdin.read()
        if not text:
            raise typer.BadParameter(f"{flag_name}: stdin closed without input")
    elif raw.startswith("@"):
        path = Path(raw[1:])
        if not path.exists() or not path.is_file():
            raise typer.BadParameter(f"{flag_name}: file '{path}' does not exist")
        text = path.read_text(encoding="utf-8")
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
