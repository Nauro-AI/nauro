"""Adapter-side embedding-flag resolution and pass-through (consumer wiring).

The kernel takes ``use_embeddings`` as a bool; the adapters own resolving it
from the environment and config. Precedence mirrors NAURO_HOME: the
``NAURO_EMBEDDINGS`` env var wins over the ``search.embeddings`` config key,
and the default is OFF.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nauro.mcp import tools as mcp_tools
from nauro.store.config import resolve_embeddings_flag, set_config


@pytest.fixture(autouse=True)
def _clear_embeddings_env(monkeypatch):
    """Ensure no stray NAURO_EMBEDDINGS leaks from the dev shell."""
    monkeypatch.delenv("NAURO_EMBEDDINGS", raising=False)


class TestResolveEmbeddingsFlag:
    def test_default_off(self):
        assert resolve_embeddings_flag() is False

    def test_env_on(self, monkeypatch):
        monkeypatch.setenv("NAURO_EMBEDDINGS", "1")
        assert resolve_embeddings_flag() is True

    def test_env_truthy_tokens(self, monkeypatch):
        for token in ("1", "true", "TRUE", "yes", "On"):
            monkeypatch.setenv("NAURO_EMBEDDINGS", token)
            assert resolve_embeddings_flag() is True

    def test_env_off_tokens(self, monkeypatch):
        for token in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("NAURO_EMBEDDINGS", token)
            assert resolve_embeddings_flag() is False

    def test_config_on_when_env_unset(self):
        set_config("search.embeddings", "true")
        assert resolve_embeddings_flag() is True

    def test_config_bool_value(self):
        set_config("search.embeddings", True)
        assert resolve_embeddings_flag() is True

    def test_env_overrides_config_off(self, monkeypatch):
        """Env wins: config ON but env OFF resolves OFF."""
        set_config("search.embeddings", "true")
        monkeypatch.setenv("NAURO_EMBEDDINGS", "0")
        assert resolve_embeddings_flag() is False

    def test_env_overrides_config_on(self, monkeypatch):
        """Env wins: config OFF (unset) but env ON resolves ON."""
        monkeypatch.setenv("NAURO_EMBEDDINGS", "1")
        assert resolve_embeddings_flag() is True


def _scaffold_store(root: Path) -> Path:
    store = root / "proj"
    (store / "decisions").mkdir(parents=True)
    return store


class TestAdapterPassThrough:
    def test_check_decision_passes_resolved_flag(self, tmp_path, monkeypatch):
        store_path = _scaffold_store(tmp_path)
        captured = {}

        def _fake_op(store, proposed_approach, context, use_embeddings=False):
            captured["use_embeddings"] = use_embeddings
            from nauro_core.operations import CheckDecisionResult

            return CheckDecisionResult(assessment="ok")

        monkeypatch.setattr(mcp_tools, "_check_decision_op", _fake_op)
        monkeypatch.setenv("NAURO_EMBEDDINGS", "1")
        mcp_tools.tool_check_decision(store_path, "Use Redis for caching")
        assert captured["use_embeddings"] is True

    def test_check_decision_default_off(self, tmp_path, monkeypatch):
        store_path = _scaffold_store(tmp_path)
        captured = {}

        def _fake_op(store, proposed_approach, context, use_embeddings=False):
            captured["use_embeddings"] = use_embeddings
            from nauro_core.operations import CheckDecisionResult

            return CheckDecisionResult(assessment="ok")

        monkeypatch.setattr(mcp_tools, "_check_decision_op", _fake_op)
        mcp_tools.tool_check_decision(store_path, "Use Redis for caching")
        assert captured["use_embeddings"] is False

    def test_search_decisions_passes_resolved_flag(self, tmp_path, monkeypatch):
        store_path = _scaffold_store(tmp_path)
        captured = {}

        def _fake_op(store, query, limit, include_superseded=False, use_embeddings=False):
            captured["use_embeddings"] = use_embeddings
            from nauro_core.operations import SearchDecisionsResult

            return SearchDecisionsResult(results=[])

        monkeypatch.setattr(mcp_tools, "_search_decisions_op", _fake_op)
        monkeypatch.setenv("NAURO_EMBEDDINGS", "1")
        mcp_tools.tool_search_decisions(store_path, "redis")
        assert captured["use_embeddings"] is True

    def test_search_decisions_default_off(self, tmp_path, monkeypatch):
        store_path = _scaffold_store(tmp_path)
        captured = {}

        def _fake_op(store, query, limit, include_superseded=False, use_embeddings=False):
            captured["use_embeddings"] = use_embeddings
            from nauro_core.operations import SearchDecisionsResult

            return SearchDecisionsResult(results=[])

        monkeypatch.setattr(mcp_tools, "_search_decisions_op", _fake_op)
        mcp_tools.tool_search_decisions(store_path, "redis")
        assert captured["use_embeddings"] is False
