"""Shared pytest helpers for the nauro-core test suite.

Hosts the decision-seeding helpers that the operations-kernel tests and the
search/embeddings tests previously duplicated per module. Plain functions
rather than fixtures: the tests call them at module scope to build shared
``DECISIONS`` lists and inline stores, mirroring how the nauro package exposes
``seed_consented_config`` and friends from its own ``tests/conftest.py``.
"""

from __future__ import annotations

from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    DecisionType,
    format_decision,
)
from nauro_core.operations import InMemoryStore


def _seed_decision(
    num: int,
    title: str,
    rationale: str = "Test rationale.",
    *,
    status: DecisionStatus = DecisionStatus.active,
    confidence: DecisionConfidence = DecisionConfidence.medium,
    decision_type: DecisionType | None = None,
    decision_date: date | None = None,
    stem: str | None = None,
) -> tuple[str, str]:
    """Return ``(file_stem, formatted_markdown)`` for a minimal v2 decision.

    The parameter set is the union of the per-module ``_seed_decision`` copies
    the operations-kernel tests carried. Every argument defaults so an existing
    call site keeps its meaning: ``decision_date`` falls back to 2026-01-01, an
    unset ``decision_type`` renders identically to omitting it, and ``stem``
    defaults to the ``NNN-slug`` form.
    """
    superseded_by = "999" if status is DecisionStatus.superseded else None
    decision = Decision(
        date=decision_date or date(2026, 1, 1),
        confidence=confidence,
        status=status,
        superseded_by=superseded_by,
        decision_type=decision_type,
        num=num,
        title=title,
        rationale=rationale,
    )
    if stem is None:
        slug = title.lower().replace(" ", "-")
        stem = f"{num:03d}-{slug}"
    return stem, format_decision(decision)


def _store_with(*decisions: tuple[str, str], **files: str) -> InMemoryStore:
    """Build an ``InMemoryStore`` from ``(stem, body)`` pairs and extra files."""
    return InMemoryStore(decisions=dict(decisions), files=dict(files))


def make_decision(num: int, title: str, rationale: str, status: str = "active") -> Decision:
    """Construct an in-memory ``Decision`` for the BM25 search-path tests."""
    status_enum = DecisionStatus(status)
    return Decision(
        date=date(2026, 4, 7),
        confidence=DecisionConfidence.medium,
        status=status_enum,
        superseded_by="999" if status_enum is DecisionStatus.superseded else None,
        num=num,
        title=title,
        rationale=rationale,
    )
