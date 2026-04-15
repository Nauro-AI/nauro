"""Tests for Tier 1 structural validation."""

from pathlib import Path

import pytest

from nauro.store.writer import append_decision
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.tier1 import (
    screen_structural,
    update_hash_index,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


class TestSchemaValidation:
    def test_rejects_empty_title(self, store):
        proposal = {"title": "", "rationale": "Some valid rationale here."}
        action, reason = screen_structural(proposal, store)
        assert action == "reject"
        assert "Title" in reason

    def test_rejects_missing_title(self, store):
        proposal = {"rationale": "Some valid rationale here."}
        action, reason = screen_structural(proposal, store)
        assert action == "reject"

    def test_rejects_empty_rationale(self, store):
        proposal = {"title": "Valid Title", "rationale": ""}
        action, reason = screen_structural(proposal, store)
        assert action == "reject"
        assert "Rationale" in reason

    def test_rejects_invalid_confidence(self, store):
        proposal = {
            "title": "Valid Title",
            "rationale": "This is a valid rationale with enough content.",
            "confidence": "super_high",
        }
        action, reason = screen_structural(proposal, store)
        assert action == "reject"
        assert "confidence" in reason.lower()

    def test_passes_valid_proposal(self, store):
        proposal = {
            "title": "Use Postgres",
            "rationale": "Better JSON support and mature ecosystem for our use case.",
            "confidence": "high",
        }
        action, reason = screen_structural(proposal, store)
        assert action == "pass"
        assert reason is None


class TestMinimumContent:
    def test_rejects_short_rationale(self, store):
        proposal = {"title": "Use Redis", "rationale": "Fast cache."}
        action, reason = screen_structural(proposal, store)
        assert action == "reject"
        assert "too short" in reason.lower()

    def test_passes_adequate_rationale(self, store):
        proposal = {
            "title": "Use Redis",
            "rationale": "Fast in-memory cache with pub/sub support for our needs.",
        }
        action, reason = screen_structural(proposal, store)
        assert action == "pass"


class TestHashDedup:
    def test_rejects_exact_duplicate(self, store):
        title = "Use Postgres for Storage"
        rationale = "Better JSON support and great ecosystem."

        # Add to hash index
        update_hash_index(title, rationale, "002-use-postgres", store)

        proposal = {"title": title, "rationale": rationale}
        action, reason = screen_structural(proposal, store)
        assert action == "reject"
        assert "duplicate" in reason.lower()

    def test_passes_different_content(self, store):
        update_hash_index("Use Postgres", "For JSON support.", "002-use-postgres", store)

        proposal = {
            "title": "Use Redis",
            "rationale": "For caching and pub/sub functionality.",
        }
        action, reason = screen_structural(proposal, store)
        assert action == "pass"

    def test_case_insensitive_hash(self, store):
        update_hash_index("Use POSTGRES", "Better JSON support.", "002-use-postgres", store)

        proposal = {
            "title": "use postgres",
            "rationale": "better json support.",
        }
        action, reason = screen_structural(proposal, store)
        assert action == "reject"


class TestTemporalDuplicate:
    def test_rejects_same_title_within_24h(self, store):
        # Write a decision with a title
        append_decision(
            store,
            "Use Postgres for Storage",
            rationale="Better JSON support and great ecosystem.",
        )

        proposal = {
            "title": "Use Postgres for Storage",
            "rationale": "A completely different rationale but same title as recent.",
        }
        action, reason = screen_structural(proposal, store)
        assert action == "reject"
        assert "same title" in reason.lower()

    def test_passes_different_title(self, store):
        append_decision(store, "Use Postgres", rationale="For JSON support in the app.")

        proposal = {
            "title": "Use Redis for Caching",
            "rationale": "Fast in-memory store for session data.",
        }
        action, reason = screen_structural(proposal, store)
        assert action == "pass"
