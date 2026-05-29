"""Write/validation integration at the tool-adapter boundary.

These exercise the transport-agnostic ``tool_*`` adapters in
``nauro.mcp.tools`` directly — the same callables the local stdio MCP surface
delegates to. The former local FastAPI HTTP surface that wrapped these was
retired; the adapters remain the single local integration point.
"""

from pathlib import Path

import pytest
from nauro_core.constants import NO_DECISIONS_TO_CHECK

from nauro.mcp.tools import (
    tool_check_decision,
    tool_flag_question,
    tool_propose_decision,
    tool_update_state,
)
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    (store_path / "stack.md").write_text("# Stack\n- Python 3.11\n")
    return store_path


def test_propose_decision_new(store):
    """Proposing a genuinely new decision auto-confirms."""
    data = tool_propose_decision(
        store,
        title="Use Redis for Caching",
        rationale="Fast in-memory store with pub/sub support for session management.",
        confidence="high",
    )
    assert data["status"] == "confirmed"
    assert "decision_id" in data


def test_propose_decision_rejected(store):
    """Proposing a decision with empty title is rejected at Tier 1."""
    data = tool_propose_decision(
        store,
        title="",
        rationale="Some valid rationale text here.",
    )
    assert data["status"] == "rejected"
    assert data["tier"] == 1


def test_propose_decision_short_rationale(store):
    """Short rationale rejected at Tier 1."""
    data = tool_propose_decision(store, title="Use Redis", rationale="Fast.")
    assert data["status"] == "rejected"


def test_check_decision_no_matches(store):
    """Checking an approach against a scaffold-only store.

    The scaffold-seeded "Initial project setup" decision is excluded from
    retrieval (mirrors tier-2 validation), so a fresh store with only that
    seed flows into the empty-state branch with the ``NO_DECISIONS_TO_CHECK``
    onboarding assessment.
    """
    data = tool_check_decision(
        store, "Use a completely novel approach to distributed tracing", None
    )
    assert data["related_decisions"] == []
    assert data["assessment"] == NO_DECISIONS_TO_CHECK
    assert "potential_conflicts" not in data


def test_check_decision_with_matches_returns_heuristic_assessment(store):
    """When BM25 finds matches, the assessment uses the locked heuristic shape."""
    from tests._writer_compat import append_decision

    append_decision(
        store,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )

    data = tool_check_decision(
        store, "Use Postgres for analytics warehouse with JSON workloads", None
    )
    assert len(data["related_decisions"]) >= 1
    assert "potential_conflicts" not in data

    assessment = data["assessment"]
    assert "Top match: D" in assessment
    assert "status active" in assessment
    assert "BM25 " in assessment
    assert "get_decision" in assessment


def test_propose_decision_with_operation_supersede(store):
    """propose_decision with operation='supersede' threads through to confirm."""
    from tests._writer_compat import append_decision

    append_decision(
        store,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )
    # supersede matches by filename stem.
    affected_id = next((store / "decisions").glob("*postgres*.md")).stem

    data = tool_propose_decision(
        store,
        title="Switch to a managed Postgres provider",
        rationale=(
            "Reduces ops burden; the rationale for self-hosting no longer applies to our scale."
        ),
        operation="supersede",
        affected_decision_id=affected_id,
        confidence="high",
    )
    # The kernel commits the supersede on the same call.
    assert data["status"] == "confirmed"
    assert data["operation"] == "supersede"


def test_flag_question(store):
    """flag_question writes the question to the store."""
    data = tool_flag_question(store, "Should we add WebSocket support?", "For real-time updates")
    assert data["status"] == "ok"

    oq = (store / "open-questions.md").read_text()
    assert "Should we add WebSocket support?" in oq


def test_update_state(store):
    """update_state writes the delta to state_current.md."""
    data = tool_update_state(store, "Deployed v0.2.0 to staging")
    assert data["status"] == "ok"

    state = (store / "state_current.md").read_text()
    assert "Deployed v0.2.0 to staging" in state


def test_propose_decision_supersede_without_affected_id_rejects(store):
    data = tool_propose_decision(
        store,
        title="Replace prior choice",
        rationale="A new choice that should replace something.",
        operation="supersede",
    )
    assert data["status"] == "rejected"
    assert data["error"]["kind"] == "rejected"
    assert "affected_decision_id" in data["error"]["reason"]


def test_propose_decision_update_without_affected_id_rejects(store):
    data = tool_propose_decision(
        store,
        title="Augment the prior choice",
        rationale="Adds nuance to an existing decision body.",
        operation="update",
    )
    assert data["status"] == "rejected"
    assert data["error"]["kind"] == "rejected"
    assert "affected_decision_id" in data["error"]["reason"]


def test_propose_supersede_with_unknown_affected_id_rejects(store):
    data = tool_propose_decision(
        store,
        title="Replace something nonexistent",
        rationale="Tests the resolution failure branch of the boundary check.",
        operation="supersede",
        affected_decision_id="decision-9999",
    )
    assert data["status"] == "rejected"
    assert data["error"]["kind"] == "rejected"
    assert "not found" in data["error"]["reason"]


def test_supersede_end_to_end_via_check_then_propose(store):
    """Natural agent workflow: read affected_decision_id from check_decision,
    pass it back to propose_decision(supersede). The id must round-trip through
    the boundary, and the store must end up with the old decision marked
    superseded and the new one active — even when Tier 2 doesn't re-surface
    similarity for the new proposal text."""
    from nauro.store.reader import _list_decisions
    from tests._writer_compat import append_decision

    append_decision(
        store,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )

    check_data = tool_check_decision(
        store, "Use SQLite for the analytics workload instead of Postgres", None
    )
    assert check_data["related_decisions"]
    affected = check_data["related_decisions"][0]["id"]

    propose_data = tool_propose_decision(
        store,
        title="Switch to SQLite for analytics",
        rationale="Lower ops burden for the read-mostly analytics workload.",
        operation="supersede",
        affected_decision_id=affected,
    )
    # The kernel commits the supersede on the same call regardless of whether
    # Tier 2 surfaces advisory similarity hits.
    assert propose_data["status"] == "confirmed"
    assert propose_data["operation"] == "supersede"

    decisions = _list_decisions(store)
    by_title = {d.title: d for d in decisions}
    assert by_title["Use Postgres as primary database"].status.value == "superseded"
    assert by_title["Switch to SQLite for analytics"].status.value == "active"
