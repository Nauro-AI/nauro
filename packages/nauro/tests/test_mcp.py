"""Tests for the MCP payload builders."""

from pathlib import Path

import pytest
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations import update_state as _update_state_op

from nauro.mcp.payloads import build_l0_payload
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.snapshot import capture_snapshot
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


def update_state(store_path: Path, delta: str) -> None:
    """Thin wrapper preserving the pre-cutover ``writer.update_state`` shape."""
    _update_state_op(FilesystemStore(store_path), delta)


def append_question(store_path: Path, question: str) -> None:
    """Thin wrapper preserving the pre-cutover ``writer.append_question`` shape."""
    _flag_question_op(FilesystemStore(store_path), question, None)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Pre-scaffolded project store with known content."""
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)

    # Add stack content
    (store_path / "stack.md").write_text(
        "# Stack\n"
        "- **Python 3.11** — primary language\n"
        "- **FastAPI** — HTTP framework\n"
        "- **PostgreSQL** — primary database\n"
        "- **Redis** — caching layer\n"
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


class TestL2Payload:
    def test_snapshot_diff(self, store: Path):
        capture_snapshot(store, trigger="first")
        append_decision(store, "New after snap 1", rationale="Testing diff")
        capture_snapshot(store, trigger="second")

        # Snapshot-diff trailer is a transport-side decoration on
        # tool_get_context, not part of the underlying context payload.
        from nauro.mcp.tools import tool_get_context

        envelope = tool_get_context(store, 2)
        assert "Snapshot Diff" in envelope["content"]

    def test_no_diff_without_snapshots(self, store: Path):
        from nauro.mcp.tools import tool_get_context

        envelope = tool_get_context(store, 2)
        assert "Snapshot Diff" not in envelope["content"]


# --- Decisions summary tests ---


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
        from tests._writer_compat import supersede_decision

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
        assert "D4 —" not in recent_section
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
