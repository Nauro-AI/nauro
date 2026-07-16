"""User-facing strings use ASCII hyphens, never em or en dashes.

CLI output and generated-file templates standardized on ASCII hyphens
(PR #341 set the direction for the status table and setup error strings;
the full sweep closed the mixed-convention inconsistency). This scan keeps
the convention enforced rather than remembered: every non-docstring string
literal under ``src/nauro`` must be free of em and en dashes.

Docstrings and comments are exempt (not user-facing). Literals that must
keep a non-ASCII dash for compatibility are allowlisted with a reason.
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "nauro"

NON_ASCII_DASHES = "—–"  # em dash, en dash

# (path relative to src/, token on the line) -> reason the dash is load-bearing.
ALLOWLIST = {
    # Byte-matched marker for legacy CLAUDE.md block cleanup; blocks with
    # this exact text are already deployed on user machines.
    ("nauro/constants.py", "NAURO_BLOCK_START"),
    # Input matching, not output: collision renumbering must recognize the
    # em-dash decision headings nauro-core writes ("# NNN — Title").
    ("nauro/sync/pull.py", "old_prefix"),
}


def _docstring_ranges(tree: ast.Module) -> list[tuple[int, int]]:
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                doc = node.body[0].value
                ranges.append((doc.lineno, doc.end_lineno))
    return ranges


def _allowlisted(rel: str, line_text: str) -> bool:
    return any(path == rel and token in line_text for path, token in ALLOWLIST)


def test_no_em_or_en_dashes_in_string_literals():
    offenders = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        source = py.read_text(encoding="utf-8")
        if not any(ch in source for ch in NON_ASCII_DASHES):
            continue
        tree = ast.parse(source, filename=str(py))
        doc_ranges = _docstring_ranges(tree)
        lines = source.split("\n")
        rel = str(py.relative_to(SRC_ROOT.parent))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not any(ch in node.value for ch in NON_ASCII_DASHES):
                continue
            if any(a <= node.lineno <= b for a, b in doc_ranges):
                continue
            if _allowlisted(rel, lines[node.lineno - 1]):
                continue
            offenders.append(f"{rel}:{node.lineno}: {node.value[:80]!r}")
    assert offenders == [], (
        "em/en dashes in user-facing string literals (use ASCII hyphens, "
        "or allowlist with a reason):\n" + "\n".join(offenders)
    )
