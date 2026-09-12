"""The cross-call parse memo in ``decision_lookup`` must be invisible to callers:
every scan returns what a fresh parse of the current bodies would, across edits,
same-size swaps, corruption, deletion, addition and restoration.
"""

from __future__ import annotations

import pytest
from conftest import _seed_decision, _store_with
from nauro_core.decision_model import parse_decision
from nauro_core.operations.decision_lookup import (
    _PARSED_BY_STEM,
    scan_decision_records,
)
from nauro_core.parsing import _decision_filename, _decision_path


@pytest.fixture(autouse=True)
def _empty_memo():
    _PARSED_BY_STEM.clear()
    yield
    _PARSED_BY_STEM.clear()


def _fresh(store) -> list[dict]:
    return [
        parse_decision(store.read_decision(stem), _decision_filename(stem)).model_dump()
        for stem in sorted(store.list_decisions())
        if _parses(store.read_decision(stem), stem)
    ]


def _parses(body: str, stem: str) -> bool:
    try:
        parse_decision(body, _decision_filename(stem))
    except Exception:
        return False
    return True


def _scanned(store) -> list[dict]:
    records, _failures, _stems = scan_decision_records(store)
    return [r.decision.model_dump() for r in records]


def test_unchanged_bodies_reuse_the_parsed_object() -> None:
    store = _store_with(_seed_decision(1, "One"), _seed_decision(2, "Two"))
    first, _, _ = scan_decision_records(store)
    second, _, _ = scan_decision_records(store)
    assert [r.decision for r in first] == [r.decision for r in second]
    assert all(a.decision is b.decision for a, b in zip(first, second, strict=True))


def test_every_kind_of_change_between_calls_is_seen() -> None:
    one_stem, one_body = _seed_decision(1, "One")
    two_stem, two_body = _seed_decision(2, "Two")
    store = _store_with((one_stem, one_body), (two_stem, two_body))
    assert _scanned(store) == _fresh(store)

    edited = one_body.replace("Test rationale.", "Edited rationale.")
    store.write_file(_decision_path(one_stem), edited)
    assert _scanned(store) == _fresh(store)
    assert _scanned(store)[0]["rationale"].startswith("Edited")

    swapped = edited.replace("Edited rationale.", "Edited rationalE.")
    store.write_file(_decision_path(one_stem), swapped)
    assert len(swapped) == len(edited)
    assert _scanned(store) == _fresh(store)

    store.write_file(_decision_path(two_stem), "---\nnot: [valid\n---\n# broken\n")
    records, failures, _ = scan_decision_records(store)
    assert [f.stem for f in failures] == [two_stem]
    assert two_stem in _PARSED_BY_STEM
    assert _PARSED_BY_STEM[two_stem][0] == two_body
    assert [r.decision.model_dump() for r in records] == _fresh(store)

    store.write_file(_decision_path(two_stem), two_body)
    assert _scanned(store) == _fresh(store)

    store.delete_file(_decision_path(two_stem))
    assert _scanned(store) == _fresh(store)
    assert two_stem not in _PARSED_BY_STEM

    three_stem, three_body = _seed_decision(3, "Three")
    store.write_file(_decision_path(three_stem), three_body)
    assert _scanned(store) == _fresh(store)
    assert set(_PARSED_BY_STEM) == {one_stem, three_stem}


def test_a_failing_parse_is_never_stored() -> None:
    stem = "005-broken"
    store = _store_with((stem, "---\nnot: [valid\n---\n# broken\n"))
    _records, failures, _ = scan_decision_records(store)
    assert [f.stem for f in failures] == [stem]
    assert stem not in _PARSED_BY_STEM
