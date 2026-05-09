"""Regression tests for lazy-import of `anthropic` + post-adopt smoke check.

The 2026-05-09 dogfood blocker was a top-level `import anthropic` guard in
`validation/tier3.py` that fired at module load time, making `anthropic` an
unconditional runtime dep for `nauro serve --stdio`.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest


# Setting sys.modules[name] = None makes Python's import machinery treat
# `import name` as a previously-failed import and raise ImportError, even
# when the package is actually installed. monkeypatch auto-restores on
# teardown so other tests in the session are unaffected.
def _block_anthropic(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)


def test_stdio_server_imports_without_anthropic(monkeypatch):
    """The stdio entry path must not require anthropic at module-load time."""
    _block_anthropic(monkeypatch)
    # Drop cached nauro.* modules so re-importing actually re-runs top-level
    # statements (otherwise Python returns the cached module and a regression
    # slips by). monkeypatch.delitem auto-restores on teardown.
    for name in [n for n in list(sys.modules) if n == "nauro" or n.startswith("nauro.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    importlib.import_module("nauro.mcp.stdio_server")


def test_evaluate_with_llm_raises_with_install_hint(monkeypatch, tmp_path: Path):
    """tier3's LLM functions sit inside a fail-closed try/except. The lazy
    import must run BEFORE that block so ImportError isn't swallowed."""
    _block_anthropic(monkeypatch)
    from nauro.validation.tier3 import evaluate_with_llm

    with pytest.raises(ImportError, match=r"pip install nauro\[extraction\]"):
        evaluate_with_llm(
            proposal={"title": "x", "rationale": "y"},
            similar_decisions=[],
            project_path=tmp_path,
            api_key="test-key",
        )


# ── Fix 4: post-adopt smoke-test of the wired binary ─────────────────────


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def test_smoke_silent_when_serve_stdio_exits_clean(monkeypatch):
    from nauro.cli.commands import adopt as adopt_mod

    monkeypatch.setattr(
        adopt_mod.subprocess,
        "run",
        lambda *a, **kw: _FakeCompletedProcess(returncode=0),
    )
    assert adopt_mod._smoke_test_wired_binary("nauro-fake") is None


def test_smoke_silent_when_serve_stdio_times_out(monkeypatch):
    """A healthy stdio server may block on stdin; timeout must be treated
    as success, NOT a crash. This is the bug the original prompt would
    have shipped."""
    from nauro.cli.commands import adopt as adopt_mod

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else "nauro", timeout=1.5)

    monkeypatch.setattr(adopt_mod.subprocess, "run", fake_run)
    assert adopt_mod._smoke_test_wired_binary("nauro-fake") is None


def test_smoke_warns_when_serve_stdio_crashes(monkeypatch):
    from nauro.cli.commands import adopt as adopt_mod

    crash_stderr = (
        "Traceback (most recent call last):\nModuleNotFoundError: No module named 'anthropic'\n"
    )
    monkeypatch.setattr(
        adopt_mod.subprocess,
        "run",
        lambda *a, **kw: _FakeCompletedProcess(returncode=1, stderr=crash_stderr),
    )
    warning = adopt_mod._smoke_test_wired_binary("nauro-fake")
    assert warning is not None
    assert "failed to start" in warning
    assert "Traceback" in warning  # surfaces first non-empty stderr line
    assert "MCP-driven flows" in warning


def test_smoke_warns_when_binary_not_found(monkeypatch):
    from nauro.cli.commands import adopt as adopt_mod

    def fake_run(*a, **kw):
        raise FileNotFoundError("nauro-fake")

    monkeypatch.setattr(adopt_mod.subprocess, "run", fake_run)
    warning = adopt_mod._smoke_test_wired_binary("nauro-fake")
    assert warning is not None
    assert "binary not found" in warning
