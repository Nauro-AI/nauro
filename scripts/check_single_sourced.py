#!/usr/bin/env python3
"""Guard against re-introducing single-sourced nauro-core helpers in a consumer.

Several pure helpers — sub sanitization, envelope-message building, decision-id
resolution, snapshot serialization — live canonically in nauro-core and are
imported by the CLI and the remote MCP server. Re-defining any of them in a
consumer package silently forks the logic, which is the exact cross-package
drift this codebase has repeatedly paid down (two of these helpers sit on
storage-key boundaries where a fork would split a user's store). This check
fails when a forbidden helper name is defined as a function anywhere in the
scanned consumer source tree, so the de-duplication cannot quietly regress.

Store-protocol methods (``read_file``, ``write_file``, ``list_decisions``,
``read_decision``, ``delete_file``) are deliberately NOT guarded: they are
legitimately re-implemented per storage backend.

Usage::

    python scripts/check_single_sourced.py packages/nauro/src [more dirs...]

Exits 0 when clean, 1 on a re-introduction, 2 on a usage error.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Pure helpers single-sourced in nauro-core. Consumers import these; they must
# not re-define them. The underscore-prefixed entries are the names the old
# local copies carried. Extend this set as more helpers are single-sourced.
FORBIDDEN: frozenset[str] = frozenset(
    {
        "sanitize_sub",
        "_sanitize_sub",
        "envelope_token_message",
        "find_decision_stem_by_id",
        "find_decision_stem_by_num",
        "resolve_decision_id",
        "_resolve_affected_decision_id",
        "serialize_snapshot",
        "normalize_snapshot",
        "snapshot_schema_version",
    }
)


def violations_in(root: Path) -> list[tuple[Path, int, str]]:
    """Return ``(file, lineno, name)`` for every forbidden function def under root."""
    found: list[tuple[Path, int, str]] = []
    for py in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError as exc:
            print(f"warning: could not parse {py}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in FORBIDDEN
            ):
                found.append((py, node.lineno, node.name))
    return found


def main(argv: list[str]) -> int:
    dirs = [Path(arg) for arg in argv[1:]] or [Path("packages/nauro/src")]
    found: list[tuple[Path, int, str]] = []
    for d in dirs:
        if not d.exists():
            print(f"error: scan path does not exist: {d}", file=sys.stderr)
            return 2
        found.extend(violations_in(d))

    if found:
        print("Single-sourced nauro-core helper(s) re-defined in a consumer:")
        for py, lineno, name in found:
            print(f"  {py}:{lineno}: {name}")
        print("\nImport these from nauro-core instead of re-defining them.")
        return 1

    scanned = ", ".join(str(d) for d in dirs)
    print(f"No re-introduced nauro-core helpers found in: {scanned}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
