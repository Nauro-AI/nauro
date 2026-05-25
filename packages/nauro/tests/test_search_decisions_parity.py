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

import json
from datetime import date
from pathlib import Path

import pytest
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)

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


def _stdio_envelope(pid: str, query: str, *, limit: int = 10) -> dict:
    # stdio search_decisions now returns a two-block list[TextContent]; the
    # JSON envelope is at content[1].text — see stdio_server module
    # docstring for the contract.
    blocks = stdio_search_decisions(query=query, limit=limit, project_id=pid)
    return json.loads(blocks[1].text)


def _tool_envelope(store_path: Path, query: str, *, limit: int = 10) -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_search_decisions(store_path, query, limit)


def test_hit_envelope_matches_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    stdio = _stdio_envelope(pid, "Auth0")
    tool = _tool_envelope(store_path, "Auth0")
    assert stdio == tool
    assert stdio["store"] == "local"
    assert "results" in stdio
    assert stdio["results"], "Auth0 query must surface at least one hit"
    hit = stdio["results"][0]
    # Locked row shape: number, title, status, score are always present;
    # date + relevance_snippet present when populated.
    for key in ("number", "title", "status", "score"):
        assert key in hit, f"missing field {key!r} in row {hit!r}"
    # The dropped envelope keys must not surface.
    assert "total_matches" not in stdio
    assert "query" not in stdio


def test_empty_store_envelope_matches_across_surfaces(empty_repo):
    pid, store_path = empty_repo
    stdio = _stdio_envelope(pid, "anything")
    tool = _tool_envelope(store_path, "anything")
    assert stdio == tool
    assert stdio == {"store": "local", "results": []}


def test_empty_query_rejection_matches_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    stdio = _stdio_envelope(pid, "")
    tool = _tool_envelope(store_path, "")
    assert stdio == tool
    assert stdio["store"] == "local"
    assert stdio["results"] == []
    assert stdio["error"]["kind"] == "rejected"
    assert "non-empty" in stdio["error"]["reason"]


def test_limit_truncates_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    stdio = _stdio_envelope(pid, "Auth0 FastAPI", limit=1)
    tool = _tool_envelope(store_path, "Auth0 FastAPI", limit=1)
    assert stdio == tool
    assert len(stdio["results"]) <= 1
