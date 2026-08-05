"""Kernel purity guard: nauro-core must never spawn processes or touch git.

The hosted Lambda bundles nauro-core, so any subprocess use here would ship
to the server. Provenance that requires git (the ``base_commit`` stamp) is
captured in the local transports and reaches the kernel only as an optional
string kwarg; this guard keeps that boundary honest by construction — no
subprocess import and no git binding (git, pygit2, dulwich) anywhere in the
kernel tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "nauro_core"

_FORBIDDEN_MODULES = frozenset({"subprocess", "git", "pygit2", "dulwich"})


def _forbidden_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(
                alias.name.split(".")[0]
                for alias in node.names
                if alias.name.split(".")[0] in _FORBIDDEN_MODULES
            )
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _FORBIDDEN_MODULES:
                found.add(module)
    return found


def test_kernel_tree_never_imports_subprocess_or_git() -> None:
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        found = _forbidden_imports(ast.parse(path.read_text(encoding="utf-8")))
        if found:
            offenders.append(f"{path.relative_to(_SRC)}: {sorted(found)}")
    assert offenders == [], (
        f"nauro-core imports process/git modules: {offenders}; process "
        "spawning and git access belong to the local transports, never the kernel."
    )
