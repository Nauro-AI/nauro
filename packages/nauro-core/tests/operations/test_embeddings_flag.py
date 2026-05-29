"""Kernel-level ``use_embeddings`` flag behavior for check/search operations.

The kernel stays I/O-free: the flag arrives as a bool argument. These tests
assert the three flag states at the operation boundary against an
``InMemoryStore``, stubbing the optional Model2Vec model rather than installing
the extra. Surface-level env/config resolution is tested in the nauro package.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

import nauro_core.embeddings as embeddings_mod
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import (
    InMemoryStore,
    check_decision,
    search_decisions,
)


def _seed(num: int, title: str, rationale: str) -> tuple[str, str]:
    decision = Decision(
        date=date(2026, 1, 1),
        confidence=DecisionConfidence.medium,
        status=DecisionStatus.active,
        num=num,
        title=title,
        rationale=rationale,
    )
    slug = title.lower().replace(" ", "-")
    return f"{num:03d}-{slug}", format_decision(decision)


def _store() -> InMemoryStore:
    return InMemoryStore(
        decisions=dict(
            [
                _seed(1, "Adopt Memcached for session cache", "Memcached for read sessions."),
                _seed(2, "Use FastAPI for the server", "FastAPI async support and OpenAPI docs."),
                _seed(
                    3,
                    "Identity tokens omit email scope",
                    "Login tokens lack the email claim so profiles never link.",
                ),
            ]
        )
    )


@pytest.fixture(autouse=True)
def _reset_model_cache():
    embeddings_mod._model = None
    embeddings_mod._load_failed = False
    yield
    embeddings_mod._model = None
    embeddings_mod._load_failed = False


class _StubModel:
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


class TestCheckDecisionFlag:
    def test_flag_off_is_default(self):
        store = _store()
        default = check_decision(store, "Add a Memcached session cache")
        explicit_off = check_decision(store, "Add a Memcached session cache", use_embeddings=False)
        assert default.model_dump() == explicit_off.model_dump()

    def test_flag_on_dep_absent_is_bm25_only(self, monkeypatch, caplog):
        import builtins

        real_import = builtins.__import__

        def _fail(name, *args, **kwargs):
            if name == "model2vec":
                raise ImportError("absent")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail)
        store = _store()
        off = check_decision(store, "Add a Memcached session cache", use_embeddings=False)
        with caplog.at_level("WARNING", logger="nauro_core.embeddings"):
            on = check_decision(store, "Add a Memcached session cache", use_embeddings=True)
        assert on.model_dump() == off.model_dump()
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    def test_flag_on_dep_present_superset(self, monkeypatch):
        store = _store()
        off = check_decision(store, "Add a Memcached session cache", use_embeddings=False)
        off_nums = [extract_num(d.id) for d in off.related_decisions]

        stub = _StubModel(
            vectors={
                "Identity tokens omit email scope": [1.0, 0.0],
                "Add a Memcached session cache": [1.0, 0.0],
            },
            default=[0.0, 1.0],
        )
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)
        on = check_decision(store, "Add a Memcached session cache", use_embeddings=True)
        on_nums = [extract_num(d.id) for d in on.related_decisions]

        assert on_nums[: len(off_nums)] == off_nums
        assert 3 in on_nums and 3 not in off_nums


class TestSearchDecisionsFlag:
    def test_flag_off_is_default(self):
        store = _store()
        default = search_decisions(store, "Memcached session")
        explicit_off = search_decisions(store, "Memcached session", use_embeddings=False)
        assert default.model_dump() == explicit_off.model_dump()

    def test_flag_on_dep_absent_is_bm25_only(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _fail(name, *args, **kwargs):
            if name == "model2vec":
                raise ImportError("absent")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail)
        store = _store()
        off = search_decisions(store, "Memcached session", use_embeddings=False)
        on = search_decisions(store, "Memcached session", use_embeddings=True)
        assert on.model_dump() == off.model_dump()

    def test_flag_on_dep_present_superset(self, monkeypatch):
        store = _store()
        off = search_decisions(store, "Memcached session", use_embeddings=False)
        off_nums = [h.number for h in off.results]

        stub = _StubModel(
            vectors={
                "Identity tokens omit email scope": [1.0, 0.0],
                "Memcached session": [1.0, 0.0],
            },
            default=[0.0, 1.0],
        )
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)
        on = search_decisions(store, "Memcached session", use_embeddings=True)
        on_nums = [h.number for h in on.results]

        assert on_nums[: len(off_nums)] == off_nums
        assert 3 in on_nums and 3 not in off_nums

    def test_embedding_hit_survives_when_bm25_fills_limit(self, monkeypatch):
        """BM25 fills ``limit`` -> the embedding-only hit must still appear.

        Without a reserved slot the embedding-only hit lands past ``limit`` and
        is sliced off, so the augmenter would contribute nothing exactly when
        BM25 already saturates the budget — the common case on a healthy corpus.
        """
        limit = 5
        # ``limit`` decisions that all share the query's lexical surface, so BM25
        # fills the budget on its own. Plus one related decision sharing no
        # surface with the query, which only the embedding pool can surface.
        decisions = {}
        for i in range(1, limit + 1):
            stem, body = _seed(
                i,
                f"Caching strategy variant {i}",
                f"Caching strategy variant {i} for the caching layer.",
            )
            decisions[stem] = body
        stem, body = _seed(
            99,
            "Identity tokens omit email scope",
            "Login tokens lack the email claim so profiles never link.",
        )
        decisions[stem] = body
        store = InMemoryStore(decisions=decisions)

        off = search_decisions(store, "caching strategy", limit=limit, use_embeddings=False)
        off_nums = [h.number for h in off.results]
        # Precondition: BM25 alone saturates the budget and never surfaces 99.
        assert len(off_nums) == limit
        assert 99 not in off_nums

        stub = _StubModel(
            vectors={
                "Identity tokens omit email scope": [1.0, 0.0],
                "caching strategy": [1.0, 0.0],
            },
            default=[0.0, 1.0],
        )
        monkeypatch.setattr(embeddings_mod, "_get_model", lambda: stub)
        on = search_decisions(store, "caching strategy", limit=limit, use_embeddings=True)
        on_nums = [h.number for h in on.results]

        # The embedding-only hit survives; the result stays within ``limit``.
        assert 99 in on_nums
        assert len(on_nums) <= limit


def extract_num(decision_id: str) -> int:
    return int(decision_id.rsplit("-", 1)[1])
