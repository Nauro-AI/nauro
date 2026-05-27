"""Negative-constraint test: no pending-state primitives remain in the kernel.

The single-call `propose_decision` flow commits on Tier 1 clean; Tier 2 hits
surface as advisory `similar_decisions` on the same response. There is no
two-step propose/confirm anymore, so the `nauro_core.pending` module and any
`_pending_store` plumbing in `propose_decision` must stay deleted.

This test guards against accidental reintroduction.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


def test_pending_module_does_not_exist() -> None:
    """`nauro_core.pending` was deleted with the trust-model relocation.

    Any reintroduction (even an empty re-import shim) means the two-step
    flow is creeping back. Fail loudly here so it surfaces in CI before
    it reaches a transport adapter.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nauro_core.pending")


def test_propose_decision_has_no_pending_store_symbol() -> None:
    """`propose_decision.py` must not carry `_pending_store` / `_get_pending_store`.

    Parses the source instead of importing the module so a stray accessor
    function still fails the guard even if it has not been called yet.
    """
    module_path = (
        Path(__file__).resolve().parent
        / ".."
        / "src"
        / "nauro_core"
        / "operations"
        / "propose_decision.py"
    ).resolve()
    tree = ast.parse(module_path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)

    forbidden = {"_pending_store", "_get_pending_store", "PendingStore"}
    leaked = forbidden & names
    assert not leaked, (
        f"pending-state primitives reappeared in propose_decision.py: {leaked}. "
        "The single-call flow has no pending store; this commits on Tier 1 clean "
        "and surfaces Tier 2 hits as advisory on the same response."
    )
