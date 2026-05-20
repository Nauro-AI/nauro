"""Regression tests for `tool_update_state` keyword-overlap warning.

The current state lives in `state_current.md`; the hardcoded `state.md`
literal silently disabled the overlap-warning branch on every modern store.
"""

from pathlib import Path

import pytest

from nauro.mcp import tools
from nauro.mcp.tools import tool_update_state
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store


def _setup_store(tmp_path) -> Path:
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    return store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_try_push", lambda _store_path: None)
    return _setup_store(tmp_path)


def test_overlap_warning_fires_on_state_current_md(store):
    (store / "state_current.md").write_text("- Implemented OAuth login flow with PKCE\n")
    result = tool_update_state(store, delta="Implemented OAuth refresh logic with PKCE")
    assert "warning" in result
    assert "keywords" in result["warning"].lower()


def test_no_warning_when_no_overlap(store):
    (store / "state_current.md").write_text("- Implemented OAuth login flow with PKCE\n")
    result = tool_update_state(store, delta="Reformatted README intro paragraph")
    assert "warning" not in result
