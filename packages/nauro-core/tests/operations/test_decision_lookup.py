"""Kernel-level tests for ``operations.decision_lookup``.

Each test seeds an :class:`~nauro_core.operations.InMemoryStore` with decision
file stems so stem resolution exercises the Store protocol without any
filesystem dependency. The id shapes mirror those pinned for
:func:`~nauro_core.parsing.extract_decision_number` in
``tests/test_parsing.py``, but here they exercise the full resolve-to-stem
path that callers rely on.
"""

from __future__ import annotations

import pytest

from nauro_core.operations import InMemoryStore, find_decision_stem_by_id
from nauro_core.operations.decision_lookup import find_decision_stem_by_num


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
