"""Tests for Tier 2 BM25 similarity validation (D93)."""

from pathlib import Path

import pytest

from nauro.store.writer import append_decision
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.tier2 import check_similarity


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


class TestCheckSimilarity:
    def test_auto_confirm_no_decisions(self, store):
        # Remove scaffold decisions
        for f in (store / "decisions").glob("*.md"):
            f.unlink()

        proposal = {
            "title": "Use Postgres",
            "rationale": "Better JSON support and mature ecosystem.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "auto_confirm"
        assert similar == []

    def test_needs_review_when_similar(self, store):
        append_decision(
            store,
            "Use Postgres for storage",
            rationale="Better JSON support and mature ecosystem for data persistence.",
        )
        proposal = {
            "title": "Use MySQL for storage",
            "rationale": "Better JSON support and ecosystem for data persistence.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "needs_review"
        assert len(similar) >= 1
        assert any("Postgres" in s["title"] for s in similar)

    def test_auto_confirm_unrelated(self, store):
        append_decision(
            store,
            "Use Postgres for storage",
            rationale="Better JSON support and mature ecosystem.",
        )
        proposal = {
            "title": "Add dark mode to the UI",
            "rationale": "Users requested a dark theme for reduced eye strain.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "auto_confirm"

    def test_vocabulary_mismatch_detected(self, store):
        """BM25 with stemming catches vocabulary mismatches (D93 motivation)."""
        append_decision(
            store,
            "Chose Memcached for session state",
            rationale="Memcached is simpler than Redis for session caching. "
            "Lower operational overhead for our read-heavy workload.",
        )
        proposal = {
            "title": "Use Redis for session caching",
            "rationale": "Redis provides session state management with persistence.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "needs_review"
        assert any("Memcached" in s["title"] for s in similar)

    def test_result_format(self, store):
        append_decision(
            store,
            "Use FastAPI for the server",
            rationale="Async support and automatic OpenAPI documentation.",
        )
        proposal = {
            "title": "Use FastAPI for the API layer",
            "rationale": "FastAPI provides async and OpenAPI docs.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "needs_review"
        hit = similar[0]
        assert "id" in hit
        assert hit["id"].startswith("decision-")
        assert "title" in hit
        assert "similarity" in hit
        assert "rationale_preview" in hit
