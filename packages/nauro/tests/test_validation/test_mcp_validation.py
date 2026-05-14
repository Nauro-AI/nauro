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
            "project_id": "testproj",
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
            "project_id": "testproj",
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
            "project_id": "testproj",
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
            "project_id": "testproj",
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
            "project_id": "testproj",
            "proposed_approach": "Use a completely novel approach to distributed tracing",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["related_decisions"] == []
    assert data["assessment"] == "No related decisions found."
    assert "potential_conflicts" not in data


@pytest.mark.asyncio
async def test_check_decision_with_matches_returns_heuristic_assessment(client, tmp_path):
    """When BM25 finds matches, the assessment uses the locked heuristic shape."""
    from nauro.store.writer import append_decision

    store_path = tmp_path / "projects" / "testproj"
    append_decision(
        store_path,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )

    resp = await client.post(
        "/check_decision",
        json={
            "project_id": "testproj",
            "proposed_approach": "Use Postgres for analytics warehouse with JSON workloads",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["related_decisions"]) >= 1
    assert "potential_conflicts" not in data

    assessment = data["assessment"]
    assert "Top match: D" in assessment
    assert "status active" in assessment
    assert "BM25 " in assessment
    assert "get_decision" in assessment


@pytest.mark.asyncio
async def test_propose_decision_with_operation_supersede(client, tmp_path):
    """propose_decision with operation='supersede' threads through to confirm."""
    from nauro.store.writer import append_decision

    store_path = tmp_path / "projects" / "testproj"
    append_decision(
        store_path,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )
    # supersede_decision matches by filename stem.
    affected_id = next((store_path / "decisions").glob("*postgres*.md")).stem

    resp = await client.post(
        "/propose_decision",
        json={
            "project_id": "testproj",
            "title": "Switch to a managed Postgres provider",
            "rationale": (
                "Reduces ops burden; the rationale for self-hosting no longer applies to our scale."
            ),
            "operation": "supersede",
            "affected_decision_id": affected_id,
            "confidence": "high",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_confirmation"
    confirm_id = data["confirm_id"]

    resp = await client.post(
        "/confirm_decision",
        json={"project_id": "testproj", "confirm_id": confirm_id},
    )
    assert resp.status_code == 200
    confirm_data = resp.json()
    assert confirm_data["status"] == "confirmed"
    assert confirm_data["operation"] == "supersede"


@pytest.mark.asyncio
async def test_flag_question_endpoint(client, tmp_path):
    """Flag question still works."""
    resp = await client.post(
        "/flag_question",
        json={
            "project_id": "testproj",
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
            "project_id": "testproj",
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
            "project_id": "testproj",
            "title": "Use SQLite for Tests",
            "rationale": "Fast and in-memory for testing purposes.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Should go through propose pipeline
    assert data["status"] in ("confirmed", "rejected", "pending_confirmation")


@pytest.mark.asyncio
async def test_propose_decision_supersede_without_affected_id_rejects(client):
    resp = await client.post(
        "/propose_decision",
        json={
            "project_id": "testproj",
            "title": "Replace prior choice",
            "rationale": "A new choice that should replace something.",
            "operation": "supersede",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert "affected_decision_id" in data["reason"]


@pytest.mark.asyncio
async def test_propose_decision_update_without_affected_id_rejects(client):
    resp = await client.post(
        "/propose_decision",
        json={
            "project_id": "testproj",
            "title": "Augment the prior choice",
            "rationale": "Adds nuance to an existing decision body.",
            "operation": "update",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert "affected_decision_id" in data["reason"]


@pytest.mark.asyncio
async def test_propose_supersede_with_unknown_affected_id_rejects(client):
    resp = await client.post(
        "/propose_decision",
        json={
            "project_id": "testproj",
            "title": "Replace something nonexistent",
            "rationale": "Tests the resolution failure branch of the boundary check.",
            "operation": "supersede",
            "affected_decision_id": "decision-9999",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert "not found" in data["reason"]


@pytest.mark.asyncio
async def test_supersede_end_to_end_via_check_then_propose(client, tmp_path):
    """Natural agent workflow: read affected_decision_id from check_decision,
    pass it back to propose_decision(supersede). The id must round-trip
    through the boundary, and the store must end up with the old decision
    marked superseded and the new one active — even when Tier 2 doesn't
    re-surface similarity for the new proposal text (Ship-blocker 2)."""
    from nauro.store.reader import _list_decisions
    from nauro.store.writer import append_decision

    store_path = tmp_path / "projects" / "testproj"
    append_decision(
        store_path,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )

    check_resp = await client.post(
        "/check_decision",
        json={
            "project_id": "testproj",
            "proposed_approach": "Use SQLite for the analytics workload instead of Postgres",
        },
    )
    assert check_resp.status_code == 200
    check_data = check_resp.json()
    assert check_data["related_decisions"]
    affected = check_data["related_decisions"][0]["id"]

    propose_resp = await client.post(
        "/propose_decision",
        json={
            "project_id": "testproj",
            "title": "Switch to SQLite for analytics",
            "rationale": "Lower ops burden for the read-mostly analytics workload.",
            "operation": "supersede",
            "affected_decision_id": affected,
        },
    )
    assert propose_resp.status_code == 200
    propose_data = propose_resp.json()
    # Either pending_confirmation (Tier 2 also surfaced similarity) or
    # confirmed (Tier 2 missed; fast path executed the supersede). Both are
    # valid; what matters is the post-state.
    if propose_data["status"] == "pending_confirmation":
        confirm_resp = await client.post(
            "/confirm_decision",
            json={"project_id": "testproj", "confirm_id": propose_data["confirm_id"]},
        )
        assert confirm_resp.status_code == 200
        confirm_data = confirm_resp.json()
        assert confirm_data["status"] == "confirmed"
        assert confirm_data["operation"] == "supersede"
    else:
        assert propose_data["status"] == "confirmed"
        assert propose_data["validation"]["operation"] == "supersede"

    decisions = _list_decisions(store_path)
    by_title = {d.title: d for d in decisions}
    assert by_title["Use Postgres as primary database"].status.value == "superseded"
    assert by_title["Switch to SQLite for analytics"].status.value == "active"
