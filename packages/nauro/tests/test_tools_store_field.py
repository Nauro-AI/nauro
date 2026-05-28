"""Tests that every local MCP tool response includes store='local'."""

from pathlib import Path

from nauro.mcp.tools import (
    tool_check_decision,
    tool_flag_question,
    tool_propose_decision,
    tool_update_state,
)
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store


def _setup_store(tmp_path, monkeypatch) -> Path:
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    return store


class TestStoreFieldPresent:
    def test_propose_decision_includes_store(self, tmp_path, monkeypatch):
        store = _setup_store(tmp_path, monkeypatch)
        result = tool_propose_decision(
            store,
            title="Use SQLite for local caching",
            rationale="SQLite is embedded and requires no server process.",
        )
        assert result["store"] == "local"

    def test_check_decision_includes_store(self, tmp_path, monkeypatch):
        store = _setup_store(tmp_path, monkeypatch)
        result = tool_check_decision(
            store,
            proposed_approach="Use a REST API for the backend",
        )
        assert result["store"] == "local"

    def test_flag_question_includes_store(self, tmp_path, monkeypatch):
        store = _setup_store(tmp_path, monkeypatch)
        result = tool_flag_question(
            store,
            question="Should we support multi-tenant stores?",
        )
        assert result["store"] == "local"

    def test_update_state_includes_store(self, tmp_path, monkeypatch):
        store = _setup_store(tmp_path, monkeypatch)
        result = tool_update_state(store, delta="Completed initial setup")
        assert result["store"] == "local"


class TestProjectIdentityPresent:
    """Every local response carries project identity alongside the store field.

    Extends the store indicator (store='local') to name the resolved project so
    a call routed to the wrong store is self-evident in the response.
    """

    def test_check_decision_includes_project_identity(self, tmp_path, monkeypatch):
        store = _setup_store(tmp_path, monkeypatch)
        result = tool_check_decision(store, proposed_approach="Use a REST API")
        # v1 store: the directory name is the project name, no separate id.
        assert result["project"] == {"id": None, "name": "testproj"}

    def test_error_envelope_also_carries_project(self, tmp_path, monkeypatch):
        missing = tmp_path / "absent-store"
        result = tool_check_decision(missing, proposed_approach="anything")
        assert result["status"] == "error"  # store-missing branch
        assert result["project"] == {"id": None, "name": "absent-store"}
