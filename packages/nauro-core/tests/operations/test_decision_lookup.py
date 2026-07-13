"""Kernel-level tests for ``operations.decision_lookup``.

Each test seeds an :class:`~nauro_core.operations.InMemoryStore` with decision
file stems so stem resolution exercises the Store protocol without any
filesystem dependency. The id shapes mirror those pinned for
:func:`~nauro_core.parsing.extract_decision_number` in
``tests/test_parsing.py``, but here they exercise the full resolve-to-stem
path that callers rely on.
"""

from __future__ import annotations

from datetime import date

import pytest

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import InMemoryStore, find_decision_stem_by_id
from nauro_core.operations.decision_lookup import (
    ParseFailure,
    find_decision_stem_by_num,
    parse_all_decisions,
    scan_decisions,
)


def _seeded_store() -> InMemoryStore:
    return InMemoryStore(
        decisions={
            "001-foo": "body",
            "042-use-postgres": "body",
        }
    )


def test_find_by_num_matches_prefix() -> None:
    assert find_decision_stem_by_num(_seeded_store(), 42) == "042-use-postgres"


def test_find_by_num_missing_number_returns_none() -> None:
    assert find_decision_stem_by_num(_seeded_store(), 999) is None


def test_find_by_num_empty_store_returns_none() -> None:
    assert find_decision_stem_by_num(InMemoryStore(), 42) is None


@pytest.mark.parametrize(
    "decision_id",
    [
        "042-use-postgres",
        "042-use-postgres.md",
        "decision-42",
        "decision-042",
        "D42",
        "D042",
        "42",
        "042",
    ],
)
def test_find_by_id_resolves_every_shape_to_stem(decision_id: str) -> None:
    assert find_decision_stem_by_id(_seeded_store(), decision_id) == "042-use-postgres"


def test_find_by_id_unparseable_returns_none() -> None:
    assert find_decision_stem_by_id(_seeded_store(), "not-a-decision") is None


def test_find_by_id_parseable_but_absent_returns_none() -> None:
    assert find_decision_stem_by_id(_seeded_store(), "decision-999") is None


# ── parse_all_decisions: order is reasserted from list_decisions ──


def _decision_body(num: int, title: str) -> str:
    """Return a well-formed v2 decision body for the given number and title."""
    return format_decision(
        Decision(
            date=date(2026, 1, 1),
            confidence=DecisionConfidence.medium,
            status=DecisionStatus.active,
            num=num,
            title=title,
            rationale=f"Rationale for {title}.",
        )
    )


class _ReversedReadDecisionsStore(InMemoryStore):
    """Store whose bulk read returns the mapping in reverse-stem order.

    ``read_decisions`` carries no ordering guarantee, so a transport is free
    to hand the mapping back in any order (a cloud fan-out finishes reads in
    completion order, not call order). This double exaggerates that by
    reversing the insertion order; a correct ``parse_all_decisions`` must
    still iterate ``list_decisions`` and yield decisions in that order, not
    the mapping's.
    """

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        return {stem: self.read_decision(stem) for stem in reversed(stems)}


def test_parse_all_decisions_reasserts_list_order_not_mapping_order() -> None:
    store = _ReversedReadDecisionsStore(
        decisions={
            "001-alpha": _decision_body(1, "Alpha"),
            "002-bravo": _decision_body(2, "Bravo"),
            "003-charlie": _decision_body(3, "Charlie"),
        }
    )
    # Sanity: the bulk read really does come back in a different order than
    # list_decisions, so the test exercises the iterate-stems guarantee.
    assert list(store.read_decisions(store.list_decisions())) == [
        "003-charlie",
        "002-bravo",
        "001-alpha",
    ]
    parsed = parse_all_decisions(store)
    assert [d.num for d in parsed] == [1, 2, 3]


# ── scan_decisions: the capturing primitive behind parse_all_decisions ──


def test_scan_decisions_returns_parsed_and_failures() -> None:
    store = InMemoryStore(
        decisions={
            "001-alpha": _decision_body(1, "Alpha"),
            "002-broken": "not a decision file",
            "003-charlie": _decision_body(3, "Charlie"),
        }
    )
    parsed, failures = scan_decisions(store)
    assert [d.num for d in parsed] == [1, 3]
    assert len(failures) == 1
    failure = failures[0]
    assert isinstance(failure, ParseFailure)
    assert failure.stem == "002-broken"
    assert failure.error


def test_parse_all_decisions_matches_scan_parsed_half() -> None:
    store = InMemoryStore(
        decisions={
            "001-alpha": _decision_body(1, "Alpha"),
            "002-broken": "not a decision file",
        }
    )
    parsed, _ = scan_decisions(store)
    assert [d.num for d in parse_all_decisions(store)] == [d.num for d in parsed]


def test_scan_decisions_clean_store_has_no_failures() -> None:
    store = InMemoryStore(decisions={"001-alpha": _decision_body(1, "Alpha")})
    parsed, failures = scan_decisions(store)
    assert [d.num for d in parsed] == [1]
    assert failures == []
