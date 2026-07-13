"""Embedding-augmented union retrieval — module invariants.

Covers the three flag states the prototype must satisfy:

1. Flag OFF -> results are byte-identical to BM25-only.
2. Flag ON + optional dependency absent -> BM25-only, with a single warning.
3. Flag ON + dependency present -> union pool is a superset of the BM25 hits.

State #3 stubs the Model2Vec model rather than installing the optional extra,
so the test exercises the real ``union_retrieve`` / ``embedding_pool`` plumbing
against a deterministic encoder.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import make_decision

import nauro_core.embeddings as embeddings_mod
from nauro_core.search import bm25_retrieve, union_retrieve

DECISIONS = [
    make_decision(
        1,
        "Use Auth0 for authentication",
        "Auth0 provides OAuth 2.1 support and handles JWT validation.",
    ),
    make_decision(
        2,
        "Chose Memcached for session state",
        "Memcached is simpler than Redis for session caching.",
    ),
    make_decision(
        3,
        "Use FastAPI for MCP server",
        "FastAPI provides async support and automatic OpenAPI docs.",
    ),
    make_decision(
        4,
        "Identity tokens carry no email scope",
        "Login tokens omit the email claim, so users land without a profile.",
    ),
]


@pytest.fixture(autouse=True)
def _reset_model_cache():
    """Clear the in-process model cache so each test sees a clean load state."""
    embeddings_mod._model = None
    embeddings_mod._load_failed = False
    yield
    embeddings_mod._model = None
    embeddings_mod._load_failed = False


class _StubModel:
    """Deterministic stand-in for a Model2Vec ``StaticModel``.

    ``encode`` returns a fixed unit vector per text keyed off a substring, so a
    chosen query lands closest to a target decision the BM25 path would miss.
    """

    def __init__(self, vectors: dict[str, list[float]], default: list[float]):
        self._vectors = vectors
        self._default = default

    def encode(self, texts):
        out = []
        for text in texts:
            vec = self._default
            for needle, v in self._vectors.items():
                if needle in text:
                    vec = v
                    break
            out.append(vec)
        return np.asarray(out, dtype=np.float32)


class TestFlagOff:
    def test_union_matches_bm25_when_flag_off(self):
        """Flag OFF -> union_retrieve is the BM25-only list, unchanged."""
        query = "authentication OAuth provider"
        bm25 = bm25_retrieve(DECISIONS, query)
        union = union_retrieve(DECISIONS, query, use_embeddings=False)
        assert union == bm25

    def test_flag_off_does_not_import_optional_dep(self, monkeypatch):
        """Flag OFF must not even attempt to load the embedding model."""
        called = False

        def _boom():
            nonlocal called
            called = True
            return None

        monkeypatch.setattr(embeddings_mod, "_get_model", _boom)
        union_retrieve(DECISIONS, "session caching", use_embeddings=False)
        assert called is False


class TestFlagOnDependencyAbsent:
    def test_union_falls_back_to_bm25_and_logs_once(self, monkeypatch, caplog):
        """Flag ON + dep absent -> BM25-only result, exactly one warning."""
        # Force the import path to fail as if the optional extra is uninstalled.
        import builtins

        real_import = builtins.__import__

        def _fail_model2vec(name, *args, **kwargs):
            if name == "model2vec":
                raise ImportError("No module named 'model2vec'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_model2vec)

        query = "authentication OAuth provider"
        bm25 = bm25_retrieve(DECISIONS, query)
        with caplog.at_level("WARNING", logger="nauro_core.embeddings"):
            first = union_retrieve(DECISIONS, query, use_embeddings=True)
            second = union_retrieve(DECISIONS, query, use_embeddings=True)

        assert first == bm25
        assert second == bm25
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1


class TestFlagOnDependencyPresent:
    def test_union_is_superset_of_bm25_hits(self, monkeypatch):
        """Flag ON + dep present -> every BM25 hit survives, embedding hits append."""
        query = "session caching"
        bm25 = bm25_retrieve(DECISIONS, query)
        bm25_nums = [h["number"] for h in bm25]

        # Make the query land closest to decision 4 (identity/email), which
        # shares no lexical surface with "session caching" -> BM25 misses it.
        stub = _StubModel(
            vectors={
                "Identity tokens carry no email scope": [1.0, 0.0],
                "session caching": [1.0, 0.0],
            },
            default=[0.0, 1.0],
        )
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)

        union = union_retrieve(DECISIONS, query, use_embeddings=True)
        union_nums = [h["number"] for h in union]

        # Superset: every BM25 hit appears in the union, in the same order.
        assert union_nums[: len(bm25_nums)] == bm25_nums
        for hit in bm25:
            assert hit in union
        # The embedding-only target was appended.
        assert 4 in union_nums
        assert 4 not in bm25_nums

    def test_embedding_only_hits_carry_null_similarity(self, monkeypatch):
        """Appended embedding hits report similarity None (no BM25 score)."""
        query = "session caching"
        stub = _StubModel(
            vectors={
                "Identity tokens carry no email scope": [1.0, 0.0],
                "session caching": [1.0, 0.0],
            },
            default=[0.0, 1.0],
        )
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)

        union = union_retrieve(DECISIONS, query, use_embeddings=True)
        appended = [h for h in union if h["number"] == 4]
        assert len(appended) == 1
        assert appended[0]["similarity"] is None
        assert appended[0]["title"] == "Identity tokens carry no email scope"

    def test_union_does_not_duplicate_shared_hits(self, monkeypatch):
        """A decision in both BM25 and embedding pools appears once."""
        query = "session caching"
        bm25 = bm25_retrieve(DECISIONS, query)
        bm25_top = bm25[0]["number"]

        # Point the embedding model at the same decision BM25 already ranked top.
        target_title = next(d.title for d in DECISIONS if d.num == bm25_top)
        stub = _StubModel(
            vectors={target_title: [1.0, 0.0], "session caching": [1.0, 0.0]},
            default=[0.0, 1.0],
        )
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)

        union = union_retrieve(DECISIONS, query, use_embeddings=True)
        union_nums = [h["number"] for h in union]
        assert union_nums.count(bm25_top) == 1


class TestEmbeddingPool:
    def test_returns_empty_when_model_unavailable(self, monkeypatch):
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: None)
        assert embeddings_mod.embedding_pool(DECISIONS, "anything", top_k=5) == []

    def test_returns_empty_for_blank_query(self, monkeypatch):
        stub = _StubModel(vectors={}, default=[1.0, 0.0])
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)
        assert embeddings_mod.embedding_pool(DECISIONS, "   ", top_k=5) == []

    def test_ranks_by_cosine_descending(self, monkeypatch):
        stub = _StubModel(
            vectors={
                "Use Auth0 for authentication": [1.0, 0.0],
                "Use FastAPI for MCP server": [0.0, 1.0],
            },
            default=[0.0, 1.0],
        )
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)
        # Query aligned with the Auth0 vector -> decision 1 ranks first.
        pool = embeddings_mod.embedding_pool(
            [d for d in DECISIONS if d.num in (1, 3)],
            "Use Auth0 for authentication",
            top_k=2,
        )
        assert pool[0] == 1
