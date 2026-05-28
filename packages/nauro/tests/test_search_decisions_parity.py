"""Surface-level parity for the local ``search_decisions`` adapters.

After the kernel cutover, every local surface that exposes
``search_decisions`` must produce the same envelope for the same arguments
against the same store. This file pins that envelope shape across the
two adapter wirings that exist today.

Two compressions vs. the ``check_decision`` parity test:

* No CLI surface. There is no ``nauro search-decisions`` command. Auto-gen
  of ``nauro tool <name>`` from MCP ToolSpecs is deferred; adding a
  hand-written CLI mirror now would be speculative.
* No FastAPI surface. The local server does not expose a
  ``/search_decisions`` endpoint. Adding one purely to mirror the parity
  shape would be speculative infrastructure; the cross-store layer-3
  test (``test_search_decisions_cross_surface``) covers local-vs-cloud
  parity once the cloud Store exists.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.renderers import RENDERERS

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp.stdio_server import search_decisions as stdio_search_decisions
from nauro.mcp.tools import tool_search_decisions
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config


def _seed(store_path: Path, *decisions: Decision) -> None:
    decisions_dir = store_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    for d in decisions:
        slug = d.title.lower().replace(" ", "-")
        (decisions_dir / f"{d.num:03d}-{slug}.md").write_text(format_decision(d))


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Seed a project with two decisions whose titles + rationale make
    BM25 searches deterministic for the parity assertions below."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("parity-search", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-search"})
    _seed(
        store_path,
        Decision(
            date=date(2026, 1, 1),
            confidence=DecisionConfidence.high,
            status=DecisionStatus.active,
            num=1,
            title="Use Auth0 for authentication",
            rationale="Auth0 provides OAuth 2.1 support and handles JWT validation.",
        ),
        Decision(
            date=date(2026, 1, 2),
            confidence=DecisionConfidence.medium,
            status=DecisionStatus.active,
            num=2,
            title="Use FastAPI for MCP server",
            rationale="FastAPI plus Mangum is the Lambda deployment combination.",
        ),
    )
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def empty_repo(tmp_path, monkeypatch):
    """Register a project with an empty store (no decisions) and chdir in."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("empty-search", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "empty-search"})
    store_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo)
    return pid, store_path


def _stdio_rendered(pid: str, query: str, *, limit: int = 10) -> str:
    """Return the rendered stdio surface text for the parity comparison.

    Renderer-scoped read tools now return a single ``content[0]`` block
    carrying the renderer output. The stdio surface participates in
    parity by emitting the same renderer output the direct tool envelope
    drives through ``RENDERERS["search_decisions"]``.
    """
    result = stdio_search_decisions(query=query, limit=limit, project_id=pid)
    assert len(result.content) == 1
    assert result.structuredContent is None
    return result.content[0].text


def _tool_envelope(store_path: Path, query: str, *, limit: int = 10) -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_search_decisions(store_path, query, limit)


def test_hit_envelope_matches_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    tool = _tool_envelope(store_path, "Auth0")
    assert _stdio_rendered(pid, "Auth0") == RENDERERS["search_decisions"](tool)
    assert tool["store"] == "local"
    assert "results" in tool
    assert tool["results"], "Auth0 query must surface at least one hit"
    hit = tool["results"][0]
    # Locked row shape: number, title, status, score are always present;
    # date + relevance_snippet present when populated.
    for key in ("number", "title", "status", "score"):
        assert key in hit, f"missing field {key!r} in row {hit!r}"
    # The dropped envelope keys must not surface.
    assert "total_matches" not in tool
    assert "query" not in tool


def test_empty_store_envelope_matches_across_surfaces(empty_repo):
    pid, store_path = empty_repo
    tool = _tool_envelope(store_path, "anything")
    assert _stdio_rendered(pid, "anything") == RENDERERS["search_decisions"](tool)
    assert tool.pop("project")["id"] == pid
    assert tool == {"store": "local", "results": []}


def test_empty_query_rejection_matches_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    tool = _tool_envelope(store_path, "")
    assert _stdio_rendered(pid, "") == RENDERERS["search_decisions"](tool)
    assert tool["store"] == "local"
    assert tool["results"] == []
    assert tool["error"]["kind"] == "rejected"
    assert "non-empty" in tool["error"]["reason"]


def test_limit_truncates_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    tool = _tool_envelope(store_path, "Auth0 FastAPI", limit=1)
    assert _stdio_rendered(pid, "Auth0 FastAPI", limit=1) == RENDERERS["search_decisions"](tool)
    assert len(tool["results"]) <= 1
