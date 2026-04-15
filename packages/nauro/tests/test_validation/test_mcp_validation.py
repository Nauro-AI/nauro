"""Tests for MCP server validation integration."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nauro.mcp.server import app
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.pending import clear_all


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    (store_path / "stack.md").write_text("# Stack\n- Python 3.11\n")
    return store_path


@pytest.fixture
def client(tmp_path: Path, monkeypatch, store) -> AsyncClient:
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    clear_all()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_pending():
    clear_all()
    yield
    clear_all()


@pytest.mark.asyncio
async def test_propose_decision_new(client, tmp_path):
    """Proposing a genuinely new decision auto-confirms."""
    resp = await client.post(
        "/propose_decision",
        json={
            "project": "testproj",
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store with pub/sub support for session management.",
            "confidence": "high",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "confirmed"
    assert "decision_id" in data


@pytest.mark.asyncio
async def test_propose_decision_rejected(client):
    """Proposing a decision with empty title is rejected at Tier 1."""
    resp = await client.post(
        "/propose_decision",
        json={
            "project": "testproj",
            "title": "",
            "rationale": "Some valid rationale text here.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert data["validation"]["tier"] == 1


@pytest.mark.asyncio
async def test_propose_decision_short_rationale(client):
    """Short rationale rejected at Tier 1."""
    resp = await client.post(
        "/propose_decision",
        json={
            "project": "testproj",
            "title": "Use Redis",
            "rationale": "Fast.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"


@pytest.mark.asyncio
async def test_confirm_decision_invalid_id(client):
    """Confirming with invalid ID returns error."""
    resp = await client.post(
        "/confirm_decision",
        json={
            "project": "testproj",
            "confirm_id": "nonexistent-uuid",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_check_decision_no_matches(client):
    """Checking an approach with no related decisions."""
    resp = await client.post(
        "/check_decision",
        json={
            "project": "testproj",
            "proposed_approach": "Use a completely novel approach to distributed tracing",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["related_decisions"] == []
    assert "proceed" in data["assessment"].lower() or "no existing" in data["assessment"].lower()


@pytest.mark.asyncio
async def test_flag_question_endpoint(client, tmp_path):
    """Flag question still works."""
    resp = await client.post(
        "/flag_question",
        json={
            "project": "testproj",
            "question": "Should we add WebSocket support?",
            "context": "For real-time updates",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    oq = (tmp_path / "projects" / "testproj" / "open-questions.md").read_text()
    assert "Should we add WebSocket support?" in oq


@pytest.mark.asyncio
async def test_update_state_endpoint(client, tmp_path):
    """Update state still works."""
    resp = await client.post(
        "/update_state",
        json={
            "project": "testproj",
            "delta": "Deployed v0.2.0 to staging",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    state = (tmp_path / "projects" / "testproj" / "state_current.md").read_text()
    assert "Deployed v0.2.0 to staging" in state


@pytest.mark.asyncio
async def test_legacy_log_decision_endpoint(client, tmp_path):
    """Legacy /log_decision still works (redirects to propose)."""
    resp = await client.post(
        "/log_decision",
        json={
            "project": "testproj",
            "title": "Use SQLite for Tests",
            "rationale": "Fast and in-memory for testing purposes.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Should go through propose pipeline
    assert data["status"] in ("confirmed", "rejected", "pending_confirmation")


@pytest.mark.asyncio
async def test_extraction_through_validation(tmp_path, monkeypatch):
    """Extraction pipeline routes through validation."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))

    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)

    from nauro.extraction.pipeline import route_extraction_to_store

    result = {
        "decisions": [
            {
                "title": "Adopt TypeScript",
                "rationale": "Type safety reduces bugs in our large codebase.",
                "confidence": "high",
                "decision_type": "pattern",
            }
        ],
        "questions": ["What about Deno?"],
        "state_delta": "Started TypeScript migration",
    }

    output = route_extraction_to_store(result, store_path, source="commit")
    assert output is not None

    # Verify decision was written
    decisions_dir = store_path / "decisions"
    files = [f.name for f in decisions_dir.glob("*.md")]
    assert any("typescript" in f.lower() for f in files)
