"""Tests for MCP server hook endpoints.

All extraction calls are mocked — no real API calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from nauro.mcp.server import app
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture
def hook_client(tmp_path: Path, monkeypatch) -> AsyncClient:
    """Async test client with a project store for hook tests."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))

    from nauro.store.registry import register_project

    repo_dir = tmp_path / "repos" / "myrepo"
    repo_dir.mkdir(parents=True)
    store_path = register_project("hookproj", [repo_dir])
    scaffold_project_store("hookproj", store_path)

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# POST /hooks/pre-compact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_compact_returns_200(hook_client):
    resp = await hook_client.post(
        "/hooks/pre-compact",
        json={
            "session_id": "sess-123",
            "cwd": "/some/path",
            "hook_event_name": "PreCompact",
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_pre_compact_empty_body(hook_client):
    resp = await hook_client.post("/hooks/pre-compact", json={})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /hooks/post-compact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_compact_returns_200_no_project(hook_client):
    """Returns 200 even when project can't be resolved."""
    resp = await hook_client.post(
        "/hooks/post-compact",
        json={
            "session_id": "sess-123",
            "cwd": "/nonexistent/path",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_project"


@pytest.mark.asyncio
async def test_post_compact_returns_200_no_session_id(hook_client, tmp_path):
    repo_dir = tmp_path / "repos" / "myrepo"
    resp = await hook_client.post(
        "/hooks/post-compact",
        json={
            "cwd": str(repo_dir),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_session_id"


@pytest.mark.asyncio
async def test_post_compact_returns_200_on_error(hook_client, tmp_path):
    """Hook must return 200 even on internal error."""
    repo_dir = tmp_path / "repos" / "myrepo"
    resp = await hook_client.post(
        "/hooks/post-compact",
        json={
            "session_id": "sess-missing",
            "cwd": str(repo_dir),
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_compact_triggers_extraction(hook_client, tmp_path):
    """When session file exists with compaction, extraction is triggered."""
    repo_dir = tmp_path / "repos" / "myrepo"

    # Mock the extraction pipeline
    with patch("nauro.mcp.server._run_post_compact_extraction") as mock_extract:
        mock_extract.return_value = {
            "status": "extracted",
            "decisions": 1,
            "questions": 0,
            "composite_score": 0.7,
        }

        resp = await hook_client.post(
            "/hooks/post-compact",
            json={
                "session_id": "sess-123",
                "cwd": str(repo_dir),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "extracted"
        assert data["decisions"] == 1


# ---------------------------------------------------------------------------
# POST /hooks/session-start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_start_returns_l0_for_startup(hook_client, tmp_path):
    repo_dir = tmp_path / "repos" / "myrepo"
    resp = await hook_client.post(
        "/hooks/session-start",
        json={
            "session_id": "sess-123",
            "cwd": str(repo_dir),
            "source": "startup",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "context" in data
    # L0 should contain some project content
    assert isinstance(data["context"], str)


@pytest.mark.asyncio
async def test_session_start_returns_context_for_compact(hook_client, tmp_path):
    repo_dir = tmp_path / "repos" / "myrepo"
    resp = await hook_client.post(
        "/hooks/session-start",
        json={
            "session_id": "sess-123",
            "cwd": str(repo_dir),
            "source": "compact",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "context" in data


@pytest.mark.asyncio
async def test_session_start_returns_200_no_project(hook_client):
    resp = await hook_client.post(
        "/hooks/session-start",
        json={
            "session_id": "sess-123",
            "cwd": "/nonexistent",
            "source": "startup",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["context"] == ""


@pytest.mark.asyncio
async def test_session_start_handles_missing_source(hook_client, tmp_path):
    """Defaults to startup behavior when source is not provided."""
    repo_dir = tmp_path / "repos" / "myrepo"
    resp = await hook_client.post(
        "/hooks/session-start",
        json={
            "session_id": "sess-123",
            "cwd": str(repo_dir),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "context" in data


# ---------------------------------------------------------------------------
# Graceful error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_never_crash_client(hook_client):
    """All hook endpoints return 200 regardless of input."""
    for endpoint in ["/hooks/pre-compact", "/hooks/post-compact", "/hooks/session-start"]:
        resp = await hook_client.post(endpoint, json={})
        assert resp.status_code == 200, f"{endpoint} returned {resp.status_code}"
