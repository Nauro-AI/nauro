#!/usr/bin/env python3
"""Enforce the project's code prose budget with a baseline that only shrinks.

A docstring says what a caller needs and stops: 3 lines for a function or method,
5 for a class, 15 for a module. Prose longer than the body it documents is a
restructure signal. The module docstring is the only place in code where a design
note belongs; history, dates, ticket references and rejected alternatives belong
in the commit message, the PR body and the project's decision store.

Recorded overruns in the baseline are not failures, but the baseline is one-way:
a new overrun fails, and an entry that stops overrunning must be deleted, so the
recorded debt can only go down.

Usage: check_docstring_budget.py [--baseline PATH] [--write-baseline] DIR...
Exits 0 when clean, 1 on a budget failure, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MODULE_BUDGET = 15
CLASS_BUDGET = 5
FUNCTION_BUDGET = 3

DEFAULT_BASELINE = Path("scripts/docstring_budget_baseline.txt")
BASELINE_HEADER = (
    "# Docstring budget baseline: pre-existing overruns, one <path>::<qualname> per line.",
    "# This list may only shrink. Fix the docstring and delete the entry.",
)

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
DEFINITION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True, order=True)
class Violation:
    """One docstring that exceeds its budget, keyed by ``<path>::<qualname>``."""

    identifier: str
    kind: str
    lines: int
    budget: int

    def describe(self) -> str:
        """Return the one-line report form of this violation."""
        return (
            f"{self.identifier}: {self.kind} docstring is "
            f"{self.lines} lines (budget {self.budget})"
        )


def docstring_lines(node: ast.AST) -> int:
    """Return the docstring's prose lines: leading and trailing blank lines do not count."""
    text = ast.get_docstring(node, clean=False)
    prose = "" if text is None else text.strip()
    return prose.count("\n") + 1 if prose else 0


def budget_and_kind(node: ast.AST) -> tuple[int, str]:
    """Return the (budget, kind) pair that applies to a module, class or function node."""
    if isinstance(node, ast.ClassDef):
        return CLASS_BUDGET, "class"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return FUNCTION_BUDGET, "function"
    return MODULE_BUDGET, "module"


def qualified_definitions(node: ast.AST, prefix: str = "") -> Iterator[tuple[Definition, str]]:
    """Yield every nested class and function under node with its dotted qualname."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, DEFINITION_TYPES):
            qualname = prefix + child.name
            yield child, qualname
            yield from qualified_definitions(child, qualname + ".")
        else:
            yield from qualified_definitions(child, prefix)


def display_path(path: Path) -> str:
    """Return path relative to the repo root, else to the working directory, else absolute."""
    resolved = path.resolve()
    for base in (Path(__file__).resolve().parents[1], Path.cwd().resolve()):
        try:
            return resolved.relative_to(base).as_posix()
        except ValueError:
            continue
    return resolved.as_posix()


def _violation_for(node: ast.AST, identifier: str) -> Violation | None:
    budget, kind = budget_and_kind(node)
    lines = docstring_lines(node)
    if lines <= budget:
        return None
    return Violation(identifier=identifier, kind=kind, lines=lines, budget=budget)


def violations_in_file(path: Path) -> list[Violation]:
    """Return every over-budget docstring in one source file, module node included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    prefix = f"{display_path(path)}::"
    candidates = [(tree, f"{prefix}<module>")]
    candidates.extend((node, prefix + qualname) for node, qualname in qualified_definitions(tree))
    found = (_violation_for(node, identifier) for node, identifier in candidates)
    return [violation for violation in found if violation is not None]


def violations_in_tree(root: Path) -> list[Violation]:
    """Return every over-budget docstring under root, skipping files that fail to parse."""
    found: list[Violation] = []
    for source in sorted(root.rglob("*.py")):
        try:
            found.extend(violations_in_file(source))
        except SyntaxError as exc:
            print(f"warning: could not parse {source}: {exc}", file=sys.stderr)
    return found


def read_baseline(path: Path) -> set[str]:
    """Return the identifiers listed in the baseline file, ignoring comments and blanks."""
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def write_baseline(path: Path, violations: list[Violation]) -> None:
    """Write the baseline file as a sorted identifier list under the standard header."""
    identifiers = sorted({violation.identifier for violation in violations})
    body = "\n".join((*BASELINE_HEADER, *identifiers))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n", encoding="utf-8")


def _report(new: list[Violation], stale: list[str], total: int, baseline: int) -> int:
    for violation in sorted(new):
        print(f"  {violation.describe()}")
    if new:
        print("\nShorten these docstrings, or move the prose to the commit message or PR body.")
    for identifier in sorted(stale):
        print(f"  {identifier}")
    if stale:
        print("\nThese entries no longer overrun. Remove them: the baseline only shrinks.")
    print(
        f"{total} over-budget docstring(s), {baseline} baselined, "
        f"{len(new)} new, {len(stale)} stale."
    )
    return 1 if new or stale else 0


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the docstring budget guard."""
    parser = argparse.ArgumentParser(description="Check docstring line budgets.")
    parser.add_argument("dirs", nargs="+", type=Path, help="source directories to scan")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="baseline file")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline file from the current violations",
    )
    return parser


def main(argv: list[str]) -> int:
    """Scan the requested directories and return the process exit code."""
    args = build_parser().parse_args(argv[1:])
    violations: list[Violation] = []
    for directory in args.dirs:
        if not directory.is_dir():
            print(f"error: scan path does not exist: {directory}", file=sys.stderr)
            return 2
        violations.extend(violations_in_tree(directory))

    if args.write_baseline:
        write_baseline(args.baseline, violations)
        print(f"Wrote {len({v.identifier for v in violations})} entries to {args.baseline}")
        return 0

    baseline = read_baseline(args.baseline)
    current = {violation.identifier for violation in violations}
    new = [violation for violation in violations if violation.identifier not in baseline]
    stale = sorted(baseline - current)
    return _report(new, stale, len(violations), len(baseline))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
