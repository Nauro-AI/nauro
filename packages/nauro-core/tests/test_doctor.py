"""Tests for ``nauro_core.doctor`` deterministic store-integrity diagnosis.

Each test seeds an :class:`~nauro_core.operations.InMemoryStore` with decision
bodies built through ``format_decision`` so the fixtures cannot drift from the
on-disk v2 format. The load-bearing case is the one-to-many retirement
convention, which must never be flagged as a contradiction.
"""

from __future__ import annotations

from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.doctor import diagnose_store
from nauro_core.operations import InMemoryStore


def _stem(num: int, slug: str = "decision") -> str:
    return f"{num:03d}-{slug}"


def _body(
    num: int,
    *,
    status: DecisionStatus = DecisionStatus.active,
    supersedes: str | None = None,
    superseded_by: str | None = None,
) -> str:
    """Return canonical v2 markdown for a decision via the shared serializer."""
    return format_decision(
        Decision(
            date=date(2026, 1, 1),
            confidence=DecisionConfidence.medium,
            status=status,
            supersedes=supersedes,
            superseded_by=superseded_by,
            num=num,
            title=f"Decision {num}",
            rationale=f"Rationale for decision {num}.",
        )
    )


# ── Unparseable files ──


def test_unparseable_file_reported_with_stem_and_error() -> None:
    store = InMemoryStore(decisions={_stem(1): "this is not a decision file"})
    diagnosis = diagnose_store(store)
    assert len(diagnosis.unparseable) == 1
    row = diagnosis.unparseable[0]
    assert row.stem == _stem(1)
    assert row.error
    assert diagnosis.is_clean is False


# ── Dangling refs ──


def test_dangling_supersedes_reported() -> None:
    store = InMemoryStore(decisions={_stem(10): _body(10, supersedes="999")})
    diagnosis = diagnose_store(store)
    assert len(diagnosis.dangling_refs) == 1
    ref = diagnosis.dangling_refs[0]
    assert (ref.source, ref.field, ref.target) == (10, "supersedes", 999)


def test_dangling_superseded_by_reported() -> None:
    store = InMemoryStore(
        decisions={
            _stem(10): _body(10, status=DecisionStatus.superseded, superseded_by="999"),
        }
    )
    diagnosis = diagnose_store(store)
    assert len(diagnosis.dangling_refs) == 1
    ref = diagnosis.dangling_refs[0]
    assert (ref.source, ref.field, ref.target) == (10, "superseded_by", 999)


def test_ref_to_unparseable_but_present_file_is_not_dangling() -> None:
    # D10 supersedes D11; D11's file is present but does not parse. Existence
    # is on-disk stems, so the ref resolves and is not dangling; D11 is only
    # reported once, as unparseable.
    store = InMemoryStore(
        decisions={
            _stem(10): _body(10, supersedes="11"),
            _stem(11, "broken"): "garbage that does not parse",
        }
    )
    diagnosis = diagnose_store(store)
    assert diagnosis.dangling_refs == []
    assert [row.stem for row in diagnosis.unparseable] == [_stem(11, "broken")]


# ── Cycles ──


def test_two_cycle_reported() -> None:
    store = InMemoryStore(
        decisions={
            _stem(5): _body(5, supersedes="6"),
            _stem(6): _body(6, supersedes="5"),
        }
    )
    diagnosis = diagnose_store(store)
    assert [c.members for c in diagnosis.cycles] == [(5, 6)]


def test_self_loop_reported() -> None:
    store = InMemoryStore(decisions={_stem(5): _body(5, supersedes="5")})
    diagnosis = diagnose_store(store)
    assert [c.members for c in diagnosis.cycles] == [(5,)]


def test_reciprocal_pair_is_not_a_cycle() -> None:
    # A normal supersession recorded on both endpoints collapses to one edge.
    store = InMemoryStore(
        decisions={
            _stem(5): _body(5, status=DecisionStatus.superseded, superseded_by="6"),
            _stem(6): _body(6, supersedes="5"),
        }
    )
    diagnosis = diagnose_store(store)
    assert diagnosis.cycles == []


# ── Status contradictions ──


def test_active_with_superseded_by_reported() -> None:
    store = InMemoryStore(
        decisions={
            _stem(7): _body(7, status=DecisionStatus.active, superseded_by="8"),
            _stem(8): _body(8),
        }
    )
    diagnosis = diagnose_store(store)
    assert len(diagnosis.contradictions) == 1
    row = diagnosis.contradictions[0]
    assert row.kind == "active_with_superseded_by"
    assert (row.decision, row.other) == (7, 8)


def test_forward_back_conflict_reported() -> None:
    # D9 supersedes D10, but D10 records superseded_by=D11 (present, != 9).
    store = InMemoryStore(
        decisions={
            _stem(9): _body(9, supersedes="10"),
            _stem(10): _body(10, status=DecisionStatus.superseded, superseded_by="11"),
            _stem(11): _body(11),
        }
    )
    diagnosis = diagnose_store(store)
    conflicts = [c for c in diagnosis.contradictions if c.kind == "forward_back_conflict"]
    assert len(conflicts) == 1
    row = conflicts[0]
    assert (row.decision, row.other, row.conflicting_with) == (9, 10, 11)


def test_one_to_many_convention_not_flagged() -> None:
    # D4 retires D2, D3, D5. Convention: one forward edge (D4 supersedes D2, the
    # reciprocal root) plus back-only superseded_by on every retired member.
    # No forward edge points at D3 or D5, so none is flagged.
    store = InMemoryStore(
        decisions={
            _stem(4): _body(4, supersedes="2"),
            _stem(2): _body(2, status=DecisionStatus.superseded, superseded_by="4"),
            _stem(3): _body(3, status=DecisionStatus.superseded, superseded_by="4"),
            _stem(5): _body(5, status=DecisionStatus.superseded, superseded_by="4"),
        }
    )
    diagnosis = diagnose_store(store)
    assert diagnosis.contradictions == []
    assert diagnosis.cycles == []
    assert diagnosis.dangling_refs == []
    assert diagnosis.is_clean is True


# ── Clean store + ordering ──


def test_clean_store_yields_empty_diagnosis() -> None:
    store = InMemoryStore(
        decisions={
            _stem(1): _body(1),
            _stem(2): _body(2, status=DecisionStatus.superseded, superseded_by="3"),
            _stem(3): _body(3, supersedes="2"),
        }
    )
    diagnosis = diagnose_store(store)
    assert diagnosis.is_clean is True
    assert diagnosis.unparseable == []
    assert diagnosis.dangling_refs == []
    assert diagnosis.cycles == []
    assert diagnosis.contradictions == []


def test_deterministic_ordering() -> None:
    # Multiple unparseable files and multiple dangling refs come back sorted
    # regardless of the store's stem order.
    store = InMemoryStore(
        decisions={
            _stem(30, "zeta"): "nope",
            _stem(10, "alpha"): "nope",
            _stem(20): _body(20, supersedes="900"),
            _stem(5): _body(5, supersedes="800"),
        }
    )
    diagnosis = diagnose_store(store)
    assert [row.stem for row in diagnosis.unparseable] == [
        _stem(10, "alpha"),
        _stem(30, "zeta"),
    ]
    assert [(r.source, r.target) for r in diagnosis.dangling_refs] == [(5, 800), (20, 900)]
