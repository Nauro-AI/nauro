"""Regression tests for the post-adopt smoke check of the wired nauro binary.

After ``nauro adopt`` writes the MCP config, it shells out to the wired
binary with ``serve --stdio`` to surface install errors immediately rather
than hours later when the agent first tries to use the tool. These tests
cover the four exit paths: clean exit, timeout (treated as success), crash,
missing binary.
"""

from __future__ import annotations

import subprocess


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
        "Traceback (most recent call last):\nModuleNotFoundError: No module named 'somedep'\n"
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
