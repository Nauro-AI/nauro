"""Tests for the Nauro MCP server and payload builders."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nauro.mcp.payloads import build_l0_payload, build_l1_payload, build_l2_payload
from nauro.mcp.server import app
from nauro.store.snapshot import capture_snapshot
from nauro.store.writer import append_decision, append_question, update_state
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Pre-scaffolded project store with known content."""
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)

    # Add stack content
    (store_path / "stack.md").write_text(
        "# Stack\n"
        "- **Python 3.11** \u2014 primary language\n"
        "- **FastAPI** \u2014 HTTP framework\n"
        "- **PostgreSQL** \u2014 primary database\n"
        "- **Redis** \u2014 caching layer\n"
    )

    # Add some decisions
    for i in range(6):
        append_decision(
            store_path,
            f"Decision {i + 1}",
            rationale=f"Rationale for decision {i + 1}. More detail here.",
            rejected=[
                {"alternative": f"Alt A{i}", "reason": f"Rejected reason for A{i}."},
                {"alternative": f"Alt B{i}", "reason": f"Rejected reason for B{i}."},
            ],
        )

    # Add some questions
    for i in range(7):
        append_question(store_path, f"Question {i + 1}?")

    # Update state
    update_state(store_path, "Implemented feature X")
    update_state(store_path, "Fixed bug in auth")

    return store_path


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> AsyncClient:
    """Async test client with a real project store."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))

    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)

    # Add minimal content
    (store_path / "stack.md").write_text("# Stack\n- Python 3.11\n")
    append_decision(store_path, "Use FastAPI", rationale="Good async support")

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# --- Payload builder tests ---


class TestL0Payload:
    def test_contains_current_state(self, store: Path):
        payload = build_l0_payload(store)
        assert "## Current State" in payload
        assert "Fixed bug in auth" in payload

    def test_contains_stack_oneliner(self, store: Path):
        payload = build_l0_payload(store)
        assert "**Stack:**" in payload
        assert "Python 3.11" in payload
        assert "FastAPI" in payload

    def test_contains_top3_questions(self, store: Path):
        payload = build_l0_payload(store)
        # Should have the 3 most recent, not all 7
        assert "Question 7?" in payload
        assert "Question 5?" in payload
        # Check we have exactly 3 question lines in the Open Questions section
        lines = [
            line for line in payload.split("\n") if "Question" in line and line.startswith("- [")
        ]
        assert len(lines) == 3

    def test_contains_decisions_summary(self, store: Path):
        payload = build_l0_payload(store)
        # All 6 active decisions should appear in the summary (limit is 10)
        for i in range(1, 7):
            assert f"Decision {i}" in payload
        # Summary uses compact format: D{num} — Title (date)
        assert "D6 —" in payload or "D006 —" in payload or "D6 — Decision 6" in payload

    def test_excludes_history(self, store: Path):
        payload = build_l0_payload(store)
        # History entries should not appear in L0
        assert "Implemented feature X" not in payload

    def test_word_count_budget(self, store: Path):
        payload = build_l0_payload(store)
        word_count = len(payload.split())
        assert 50 <= word_count <= 1000, f"L0 word count {word_count} outside expected range"


class TestL1Payload:
    def test_contains_full_stack(self, store: Path):
        payload = build_l1_payload(store)
        assert "# Stack" in payload
        assert "Python 3.11" in payload

    def test_contains_last10_decisions(self, store: Path):
        payload = build_l1_payload(store)
        # We have 6 decisions, all should be present
        for i in range(1, 7):
            assert f"Decision {i}" in payload

    def test_decisions_have_rationale(self, store: Path):
        payload = build_l1_payload(store)
        assert "Rationale" in payload

    def test_contains_full_questions(self, store: Path):
        payload = build_l1_payload(store)
        assert "Open Questions" in payload
        for i in range(1, 8):
            assert f"Question {i}?" in payload


class TestL2Payload:
    def test_contains_all_decisions(self, store: Path):
        payload = build_l2_payload(store)
        for i in range(1, 7):
            assert f"Decision {i}" in payload

    def test_contains_questions(self, store: Path):
        payload = build_l2_payload(store)
        assert "Open Questions" in payload

    def test_snapshot_diff(self, store: Path):
        capture_snapshot(store, trigger="first")
        append_decision(store, "New after snap 1", rationale="Testing diff")
        capture_snapshot(store, trigger="second")

        payload = build_l2_payload(store)
        assert "Snapshot Diff" in payload

    def test_no_diff_without_snapshots(self, store: Path):
        payload = build_l2_payload(store)
        assert "Snapshot Diff" not in payload


# --- Decisions summary tests (D77) ---


class TestL0DecisionsSummary:
    """Tests for the L0 decisions summary enrichment."""

    def test_l0_includes_summary_up_to_10(self, tmp_path: Path):
        """L0 includes decisions summary with up to 10 entries."""
        store_path = tmp_path / "projects" / "testproj"
        scaffold_project_store("testproj", store_path)
        # scaffold creates 001-initial-setup, so these will be 002-016
        for i in range(15):
            append_decision(store_path, f"Decision {i + 1}", rationale=f"Rationale {i + 1}")
        payload = build_l0_payload(store_path)
        # Should have exactly 10 entries in the summary (most recent)
        recent_section = payload[payload.index("## Recent Decisions") :]
        summary_lines = [line for line in recent_section.split("\n") if line.startswith("- D")]
        assert len(summary_lines) == 10
        # Most recent first (16 total: 001 scaffold + 002-016)
        assert "D16" in summary_lines[0]
        assert "D7" in summary_lines[-1]

    def test_l0_summary_excludes_superseded(self, tmp_path: Path):
        """L0 summary shows only active decisions (superseded excluded)."""
        from nauro.store.writer import supersede_decision

        store_path = tmp_path / "projects" / "testproj"
        scaffold_project_store("testproj", store_path)
        # scaffold creates 001, these are 002-006.
        paths = []
        for i in range(5):
            paths.append(
                append_decision(store_path, f"Decision {i + 1}", rationale=f"Rationale {i + 1}")
            )
        # Mark decision 004 as superseded via the v2 writer (creates 007 as replacement).
        d4 = paths[2]
        supersede_decision(
            d4.stem,
            {"title": "Replacement for D4", "rationale": "Supersedes D4."},
            store_path,
        )

        payload = build_l0_payload(store_path)
        recent_section = payload[payload.index("## Recent Decisions") :]
        assert "D4 \u2014" not in recent_section
        summary_lines = [line for line in recent_section.split("\n") if line.startswith("- D")]
        # 6 original + 1 replacement = 7; D4 is superseded → 6 active shown.
        assert len(summary_lines) == 6

    def test_l0_summary_empty_store(self, tmp_path: Path):
        """L0 summary is empty when store has no decisions."""
        store_path = tmp_path / "projects" / "testproj"
        scaffold_project_store("testproj", store_path)
        # Remove the scaffold decision so the store has zero decisions
        for f in (store_path / "decisions").glob("*.md"):
            f.unlink()
        payload = build_l0_payload(store_path)
        assert "Recent Decisions" not in payload

    def test_l0_summary_includes_date(self, tmp_path: Path):
        """L0 summary entries include the date."""
        store_path = tmp_path / "projects" / "testproj"
        scaffold_project_store("testproj", store_path)
        append_decision(store_path, "Test Decision", rationale="Test rationale")
        payload = build_l0_payload(store_path)
        # Date should be in YYYY-MM-DD format (D2 because scaffold creates D1)
        import re

        assert re.search(r"D2 — Test Decision \(\d{4}-\d{2}-\d{2}\)", payload)

    def test_l0_summary_respects_limit(self, tmp_path: Path):
        """Summary respects the 10-decision limit."""
        store_path = tmp_path / "projects" / "testproj"
        scaffold_project_store("testproj", store_path)
        # scaffold creates 001, these are 002-021 (21 total)
        for i in range(20):
            append_decision(store_path, f"Decision {i + 1}", rationale=f"Rationale {i + 1}")
        payload = build_l0_payload(store_path)
        recent_section = payload[payload.index("## Recent Decisions") :]
        summary_lines = [line for line in recent_section.split("\n") if line.startswith("- D")]
        assert len(summary_lines) == 10


class TestL1DecisionsSummary:
    """Tests for the L1 decisions summary (beyond full decisions)."""

    def test_l1_summary_no_duplicates(self, tmp_path: Path):
        """L1 summary covers decisions beyond those shown in full."""
        store_path = tmp_path / "projects" / "testproj"
        scaffold_project_store("testproj", store_path)
        # scaffold creates 001, these are 002-016 (16 total active)
        for i in range(15):
            append_decision(store_path, f"Decision {i + 1}", rationale=f"Rationale {i + 1}")
        payload = build_l1_payload(store_path)
        # Should have "Earlier Decisions" section
        assert "## Earlier Decisions" in payload
        earlier_section = payload[payload.index("## Earlier Decisions") :]
        # Most recent 10 (D16-D7) shown in full, earlier covers D6-D1
        for i in range(1, 7):
            assert f"D{i} —" in earlier_section
        # Decisions shown in full should NOT be in the earlier summary
        for i in range(7, 17):
            assert f"D{i} —" not in earlier_section

    def test_l1_no_earlier_section_when_few_decisions(self, tmp_path: Path):
        """No Earlier Decisions section when all decisions fit in full."""
        store_path = tmp_path / "projects" / "testproj"
        scaffold_project_store("testproj", store_path)
        # scaffold creates 001, these are 002-006 (6 total, under limit of 10)
        for i in range(5):
            append_decision(store_path, f"Decision {i + 1}", rationale=f"Rationale {i + 1}")
        payload = build_l1_payload(store_path)
        assert "## Earlier Decisions" not in payload


# --- API endpoint tests ---


@pytest.mark.asyncio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_context_l0(client):
    resp = await client.post("/context", json={"project": "testproj", "level": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == 0
    assert "content" in data


@pytest.mark.asyncio
async def test_context_l1(client):
    resp = await client.post("/context", json={"project": "testproj", "level": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == 1
    assert isinstance(data["content"], str) and len(data["content"]) > 0


@pytest.mark.asyncio
async def test_context_l2(client):
    resp = await client.post("/context", json={"project": "testproj", "level": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == 2
    assert isinstance(data["content"], str) and len(data["content"]) > 0


@pytest.mark.asyncio
async def test_context_invalid_level(client):
    resp = await client.post("/context", json={"project": "testproj", "level": 5})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_context_missing_project(client):
    resp = await client.post("/context", json={"level": 0})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_log_decision_endpoint(client, tmp_path):
    resp = await client.post(
        "/log_decision",
        json={
            "project": "testproj",
            "title": "Use SQLite for tests",
            "rationale": "Fast and in-memory database that doesn't require"
            " a separate server process",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Now goes through propose pipeline
    assert data["status"] in ("confirmed", "pending_confirmation")

    if data["status"] == "confirmed":
        # Verify the decision was written
        decisions_dir = tmp_path / "projects" / "testproj" / "decisions"
        assert any("use-sqlite-for-tests" in f.name for f in decisions_dir.glob("*.md"))

        # Verify snapshot was triggered
        snapshots_dir = tmp_path / "projects" / "testproj" / "snapshots"
        assert len(list(snapshots_dir.glob("v*.json"))) >= 1


@pytest.mark.asyncio
async def test_flag_question_endpoint(client, tmp_path):
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

    # Verify question was written
    oq = (tmp_path / "projects" / "testproj" / "open-questions.md").read_text()
    assert "Should we add WebSocket support?" in oq


@pytest.mark.asyncio
async def test_update_state_endpoint(client, tmp_path):
    resp = await client.post(
        "/update_state",
        json={
            "project": "testproj",
            "delta": "Deployed v0.2.0 to staging",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Verify state was updated (now writes to state_current.md)
    state = (tmp_path / "projects" / "testproj" / "state_current.md").read_text()
    assert "Deployed v0.2.0 to staging" in state


@pytest.mark.asyncio
async def test_context_with_cwd_resolution(tmp_path, monkeypatch):
    """Test resolving project from cwd parameter."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))

    from nauro.store.registry import register_project

    repo_dir = tmp_path / "repos" / "myrepo"
    repo_dir.mkdir(parents=True)

    store_path = register_project("cwdproj", [repo_dir])
    scaffold_project_store("cwdproj", store_path)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/context", json={"cwd": str(repo_dir), "level": 0})
        assert resp.status_code == 200
        assert resp.json()["level"] == 0
