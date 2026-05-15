"""Baseline test: the validation pipeline never imports anthropic.

After Decision B (extraction retirement) the only Anthropic dependency in
Nauro was extraction itself; with extraction removed, no production code path
imports ``anthropic``. This test guards that property by simulating an env
where ``anthropic`` cannot be imported and exercising propose_decision
against overlapping titles — the path that originally surfaced the leak in
the 2026-05-09 dogfood (Finding 1, when Tier 3 still lived).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nauro.store.writer import append_decision
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.pipeline import validate_proposed_write


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "noanthropic"
    scaffold_project_store("noanthropic", store_path)
    # Seed an existing decision so subsequent proposals trigger Tier 2 hits.
    append_decision(
        store_path,
        "Use Postgres as primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="high",
        decision_type="data_model",
    )
    return store_path


def test_propose_decision_without_anthropic(store, monkeypatch):
    """≥5 propose_decision calls with overlapping titles must succeed without
    ImportError; at least one must return pending_confirmation with BM25 hits."""
    monkeypatch.setitem(sys.modules, "anthropic", None)

    titles = [
        "Use Postgres for analytics warehouse",
        "Use Postgres for read replicas",
        "Use Postgres with logical replication",
        "Use Postgres for the primary write path",
        "Use Postgres with managed extensions",
    ]
    rationale_template = (
        "Adds a use case for the existing Postgres choice; "
        "rationale variant {i} for the regression test fixture."
    )

    pending_count = 0
    for i, title in enumerate(titles):
        result = validate_proposed_write(
            {
                "title": title,
                "rationale": rationale_template.format(i=i),
                "confidence": "high",
            },
            store,
        )
        assert result.status in ("confirmed", "pending_confirmation"), (
            f"Unexpected status {result.status!r} on call {i}"
        )
        if result.status == "pending_confirmation":
            pending_count += 1
            assert result.similar_decisions, f"Pending result on call {i} should carry BM25 hits"

    assert pending_count >= 1, (
        "At least one of the five overlapping-title proposals should have "
        "surfaced a BM25 near-neighbour and returned pending_confirmation."
    )
