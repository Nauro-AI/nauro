"""Kernel-level tests for ``operations.list_decisions`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` and asserts on the typed
:class:`ListDecisionsResult` directly. Surface-level wiring tests live in
each transport's own suite.
"""

from __future__ import annotations

from conftest import _seed_decision, _store_with

from nauro_core.decision_model import (
    DecisionConfidence,
    DecisionStatus,
    DecisionType,
)
from nauro_core.operations import (
    DecisionSummary,
    InMemoryStore,
    ListDecisionsResult,
    list_decisions,
)


def test_returns_result_type() -> None:
    result = list_decisions(InMemoryStore())
    assert isinstance(result, ListDecisionsResult)


def test_empty_store_returns_empty_decisions() -> None:
    result = list_decisions(InMemoryStore())
    assert result == ListDecisionsResult(decisions=[])
    assert result.decisions == []


def test_single_decision_returns_single_row_with_correct_fields() -> None:
    stem, body = _seed_decision(
        7,
        "Adopt Redis",
        "Use Redis for session cache.",
        decision_type=DecisionType.infrastructure,
        confidence=DecisionConfidence.high,
    )
    store = _store_with((stem, body))
    result = list_decisions(store)
    assert len(result.decisions) == 1
    row = result.decisions[0]
    assert row.number == 7
    assert row.title == "Adopt Redis"
    assert row.date == "2026-01-01"
    assert row.status == "active"
    assert row.type == "infrastructure"
    assert row.confidence == "high"


def test_multiple_decisions_sorted_descending_by_number() -> None:
    seeded = [
        _seed_decision(1, "First"),
        _seed_decision(2, "Second"),
        _seed_decision(3, "Third"),
    ]
    store = _store_with(*seeded)
    result = list_decisions(store)
    numbers = [row.number for row in result.decisions]
    assert numbers == [3, 2, 1]


def test_limit_truncates_to_requested_size() -> None:
    seeded = [_seed_decision(i, f"Decision {i}") for i in range(1, 11)]
    store = _store_with(*seeded)
    result = list_decisions(store, limit=5)
    assert len(result.decisions) == 5
    # The five highest numbers come back, sorted descending.
    assert [row.number for row in result.decisions] == [10, 9, 8, 7, 6]


def test_negative_limit_returns_empty_not_negative_slice() -> None:
    # A negative limit is out of domain. Clamp to an empty result rather than
    # letting the Python negative slice silently drop the oldest rows
    # (decisions[:-1] would return all-but-one).
    seeded = [_seed_decision(i, f"Decision {i}") for i in range(1, 6)]
    store = _store_with(*seeded)
    result = list_decisions(store, limit=-1)
    assert result.decisions == []


def test_include_superseded_false_filters_out_superseded_rows() -> None:
    active = _seed_decision(1, "Active one")
    superseded = _seed_decision(2, "Old one", status=DecisionStatus.superseded)
    store = _store_with(active, superseded)
    result = list_decisions(store)
    numbers = [row.number for row in result.decisions]
    assert numbers == [1]
    assert all(row.status == "active" for row in result.decisions)


def test_include_superseded_true_retains_superseded_rows() -> None:
    active = _seed_decision(1, "Active one")
    superseded = _seed_decision(2, "Old one", status=DecisionStatus.superseded)
    store = _store_with(active, superseded)
    result = list_decisions(store, include_superseded=True)
    numbers = [row.number for row in result.decisions]
    assert numbers == [2, 1]
    statuses = {row.status for row in result.decisions}
    assert statuses == {"active", "superseded"}


def test_exclude_none_strips_unset_type_on_row() -> None:
    """A row whose underlying ``decision_type`` is unset omits ``type`` on dump."""
    stem, body = _seed_decision(5, "No type set", decision_type=None)
    store = _store_with((stem, body))
    result = list_decisions(store)
    row = result.decisions[0]
    assert row.type is None
    dumped = row.model_dump(mode="json", exclude_none=True)
    assert "type" not in dumped
    assert dumped == {
        "number": 5,
        "title": "No type set",
        "date": "2026-01-01",
        "status": "active",
        "confidence": "medium",
    }


def test_exclude_none_on_summary_with_no_date() -> None:
    """A summary built with both optionals unset serializes without them."""
    row = DecisionSummary(
        number=9,
        title="Hand-built",
        status="active",
        confidence="medium",
    )
    dumped = row.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "number": 9,
        "title": "Hand-built",
        "status": "active",
        "confidence": "medium",
    }


def test_store_field_absent_from_result_model_dump() -> None:
    """Transports own the ``store`` field; the kernel never emits it."""
    result = list_decisions(InMemoryStore())
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped
