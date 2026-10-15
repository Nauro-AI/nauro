"""Kernel purity guard: nauro-core must never spawn processes or touch git.

The hosted Lambda bundles nauro-core, so any subprocess use here would ship
to the server. Provenance that requires git (the ``base_commit`` stamp) is
captured in the local transports and reaches the kernel only as an optional
string kwarg; this guard keeps that boundary honest by construction — no
subprocess import and no git binding (git, pygit2, dulwich) anywhere in the
kernel tree. The write and diff operations also stay off pathlib and
filelock: the Store protocol abstracts paths as strings and owns locking.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "nauro_core"

_FORBIDDEN_MODULES = frozenset({"subprocess", "git", "pygit2", "dulwich"})
_STORE_ABSTRACTED_MODULES = frozenset({"pathlib", "filelock"})


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    return roots


def test_kernel_tree_never_imports_subprocess_or_git() -> None:
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        found = _imported_roots(path) & _FORBIDDEN_MODULES
        if found:
            offenders.append(f"{path.relative_to(_SRC)}: {sorted(found)}")
    assert offenders == [], (
        f"nauro-core imports process/git modules: {offenders}; process "
        "spawning and git access belong to the local transports, never the kernel."
    )


@pytest.mark.parametrize(
    "operation", ["diff_since_last_session", "update_state", "flag_question", "propose_decision"]
)
def test_operation_never_imports_pathlib_or_filelock(operation: str) -> None:
    found = _imported_roots(_SRC / "operations" / f"{operation}.py") & _STORE_ABSTRACTED_MODULES
    assert found == set(), (
        f"operations/{operation}.py imports {sorted(found)}; the Store protocol "
        "abstracts paths as strings and owns locking."
    )
