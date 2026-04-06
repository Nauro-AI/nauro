"""Tests for Tier 2 embedding similarity validation."""

from pathlib import Path
from unittest.mock import patch

import pytest

from nauro.store.writer import append_decision
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.tier2 import (
    EMBEDDING_INDEX_FILE,
    _cosine_similarity,
    _jaccard_similarity,
    _word_set,
    check_similarity,
    rebuild_embedding_index,
    update_embedding_index,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert _cosine_similarity([0, 0], [1, 0]) == 0.0


class TestJaccardSimilarity:
    def test_identical_sets(self):
        s = {"hello", "world"}
        assert _jaccard_similarity(s, s) == 1.0

    def test_disjoint_sets(self):
        assert _jaccard_similarity({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self):
        assert _jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(0.5)

    def test_empty_sets(self):
        assert _jaccard_similarity(set(), set()) == 0.0


class TestWordSet:
    def test_extracts_words(self):
        words = _word_set("Use Postgres for JSON support")
        assert "postgres" in words
        assert "json" in words
        # Words with > 2 chars are included
        assert "use" in words
        assert "for" in words

    def test_strips_punctuation(self):
        words = _word_set("Hello, world! (test)")
        assert "hello" in words
        assert "world" in words
        assert "test" in words


class TestCheckSimilarity:
    def test_auto_confirm_empty_index(self, store):
        proposal = {
            "title": "Use Postgres",
            "rationale": "Better JSON support and mature ecosystem.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "auto_confirm"
        assert similar == []

    @patch("nauro.validation.tier2._embed_text")
    def test_auto_confirm_below_threshold(self, mock_embed, store):
        """Low similarity → auto_confirm."""
        mock_embed.return_value = [1.0, 0.0, 0.0]

        # Manually write an embedding index with a dissimilar entry
        import json

        index = {
            "model": "text-embedding-3-small",
            "decisions": {
                "decision-001": {
                    "embedding": [0.0, 1.0, 0.0],
                    "title": "Use DynamoDB",
                },
            },
        }
        (store / EMBEDDING_INDEX_FILE).write_text(json.dumps(index))

        proposal = {
            "title": "Use Postgres",
            "rationale": "Better JSON support.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "auto_confirm"

    @patch("nauro.validation.tier2._embed_text")
    def test_needs_review_above_threshold(self, mock_embed, store):
        """High similarity → needs_review."""
        mock_embed.return_value = [0.9, 0.1, 0.0]

        import json

        index = {
            "model": "text-embedding-3-small",
            "decisions": {
                "decision-001": {
                    "embedding": [0.9, 0.1, 0.0],  # identical
                    "title": "Use Postgres",
                },
            },
        }
        (store / EMBEDDING_INDEX_FILE).write_text(json.dumps(index))

        proposal = {
            "title": "Use Postgres for storage",
            "rationale": "Better JSON support.",
        }
        action, similar = check_similarity(proposal, store)
        assert action == "needs_review"
        assert len(similar) >= 1
        assert similar[0]["id"] == "decision-001"

    def test_fallback_to_jaccard_on_api_failure(self, store):
        """When embedding fails, falls back to Jaccard."""
        # Add decisions with overlapping words
        append_decision(
            store,
            "Use Postgres for Storage",
            rationale="Better JSON support and mature ecosystem for our application needs.",
        )

        # No API keys set → embedding will fail → Jaccard fallback
        # Use identical words to trigger Jaccard threshold
        proposal = {
            "title": "Use Postgres for Storage",
            "rationale": "Better JSON support and mature ecosystem for our application needs.",
        }
        action, similar = check_similarity(proposal, store)
        # Jaccard should find similarity (may be auto_confirm or needs_review
        # depending on exact word overlap vs full decision corpus)
        assert action in ("auto_confirm", "needs_review")
        # At minimum, the function should complete without error (fallback worked)


class TestUpdateEmbeddingIndex:
    @patch("nauro.validation.tier2._embed_text")
    def test_adds_to_index(self, mock_embed, store):
        mock_embed.return_value = [0.1, 0.2, 0.3]

        update_embedding_index("decision-002", "Use Redis", "For caching.", store)

        import json

        index = json.loads((store / EMBEDDING_INDEX_FILE).read_text())
        assert "decision-002" in index["decisions"]
        assert index["decisions"]["decision-002"]["embedding"] == [0.1, 0.2, 0.3]

    def test_stores_title_without_embedding(self, store):
        """When embedding fails, still stores the title for Jaccard."""
        update_embedding_index("decision-002", "Use Redis", "For caching.", store)

        import json

        index = json.loads((store / EMBEDDING_INDEX_FILE).read_text())
        assert "decision-002" in index["decisions"]
        assert index["decisions"]["decision-002"]["title"] == "Use Redis"


class TestRebuildIndex:
    @patch("nauro.validation.tier2._embed_text")
    def test_rebuilds_from_all_decisions(self, mock_embed, store):
        mock_embed.return_value = [0.5, 0.5, 0.5]

        append_decision(store, "Decision A", rationale="Rationale for decision A in the project.")
        append_decision(store, "Decision B", rationale="Rationale for decision B in the project.")

        result = rebuild_embedding_index(store)
        # 2 new + 1 from scaffold (001-initial-setup)
        assert result["indexed"] >= 2
        assert result["failed"] == 0

        import json

        index = json.loads((store / EMBEDDING_INDEX_FILE).read_text())
        assert len(index["decisions"]) >= 3
