"""A single malformed decision file must not take down the read path.

The decision parser is strict by design (``decision_model.parse_decision``
raises on any deviation from the canonical v2 format). Tolerance for a stray
unparseable file on disk — a half-written body, a pre-v2 file left during a
migration — lives at the scan layer: the five scans skip-and-continue while
the targeted ``get_decision`` returns a typed error rather than crashing.

These tests seed an :class:`InMemoryStore` with one well-formed decision and
one malformed file (missing the leading ``---`` frontmatter fence, which
raises ``ValueError``) and assert every read path returns the good decision
without raising. The ``check_decision`` seed-filter asymmetry is exercised
here too: ``check_decision`` drops the scaffold seed, ``search_decisions``
does not.
"""

from __future__ import annotations

from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import (
    CheckDecisionResult,
    GetContextResult,
    GetDecisionResult,
    InMemoryStore,
    ListDecisionsResult,
    SearchDecisionsResult,
    check_decision,
    get_context,
    get_decision,
    list_decisions,
    search_decisions,
)
from nauro_core.operations.decision_lookup import parse_all_decisions

# A body with no leading ``---`` fence; ``parse_decision`` raises ValueError on
# it ("missing YAML frontmatter"). Stem is a well-formed ``NNN-slug`` so the
# scans reach the read/parse step rather than skipping it for another reason.
_MALFORMED_STEM = "042-corrupt-decision"
_MALFORMED_BODY = "# 042 — Corrupt decision\n\nNo frontmatter fence here.\n"

# Number a caller can target with get_decision; matches _MALFORMED_STEM.
_MALFORMED_NUM = 42


def _good_decision(
    num: int = 7,
    title: str = "Adopt Redis cache",
    rationale: str = "Use Redis for the session cache layer.",
) -> tuple[str, str]:
    """Return (file_stem, formatted markdown) for a parseable v2 decision."""
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


def _scaffold_seed() -> tuple[str, str]:
    """Return the scaffold-seed bookkeeping decision (num==1, fixed title)."""
    decision = Decision(
        date=date(2026, 1, 1),
        confidence=DecisionConfidence.medium,
        status=DecisionStatus.active,
        num=1,
        title="Initial project setup",
        rationale="Scaffold the project store.",
    )
    return "001-initial-project-setup", format_decision(decision)


def _store_with_good_and_malformed() -> tuple[InMemoryStore, str, str]:
    """Seed a store with one good decision and one malformed file.

    Returns the store plus the good decision's stem and body for assertions.
    """
    good_stem, good_body = _good_decision()
    store = InMemoryStore(
        decisions={
            good_stem: good_body,
            _MALFORMED_STEM: _MALFORMED_BODY,
        }
    )
    return store, good_stem, good_body


# ── parse_all_decisions: the shared guarded scan ──


def test_parse_all_decisions_skips_malformed_returns_only_good() -> None:
    store, _, _ = _store_with_good_and_malformed()
    parsed = parse_all_decisions(store)
    assert [d.num for d in parsed] == [7]
    assert parsed[0].title == "Adopt Redis cache"


# ── list_decisions ──


def test_list_decisions_returns_only_good_does_not_raise() -> None:
    store, _, _ = _store_with_good_and_malformed()
    result = list_decisions(store)
    assert isinstance(result, ListDecisionsResult)
    assert [row.number for row in result.decisions] == [7]
    assert result.decisions[0].title == "Adopt Redis cache"


# ── search_decisions ──


def test_search_decisions_matches_good_does_not_raise() -> None:
    store, _, _ = _store_with_good_and_malformed()
    result = search_decisions(store, "Redis cache")
    assert isinstance(result, SearchDecisionsResult)
    assert result.error is None
    assert 7 in {hit.number for hit in result.results}


# ── check_decision ──


def test_check_decision_returns_good_does_not_raise() -> None:
    store, _, _ = _store_with_good_and_malformed()
    result = check_decision(store, "Adopt Redis cache")
    assert isinstance(result, CheckDecisionResult)
    assert result.error is None
    assert 7 in {extract for extract in _related_numbers(result)}


def test_check_decision_filters_scaffold_seed_search_does_not() -> None:
    """Locks the by-design asymmetry: ``check_decision`` drops the scaffold
    seed (num==1 / "Initial project setup"); ``search_decisions`` does not."""
    seed_stem, seed_body = _scaffold_seed()
    store = InMemoryStore(decisions={seed_stem: seed_body})

    # check_decision drops the seed: with only the seed present it reports the
    # empty-store assessment rather than surfacing the seed as a related hit.
    checked = check_decision(store, "Initial project setup scaffold")
    assert _related_numbers(checked) == []

    # search_decisions retains the seed: a query matching it returns it.
    searched = search_decisions(store, "Initial project setup")
    assert 1 in {hit.number for hit in searched.results}


def _related_numbers(result: CheckDecisionResult) -> list[int]:
    """Extract the decision numbers from a check result's related list."""
    numbers: list[int] = []
    for related in result.related_decisions:
        # id is the canonical ``decision-NNN`` form.
        numbers.append(int(related.id.rsplit("-", 1)[-1]))
    return numbers


# ── get_context ──


def test_get_context_assembles_at_every_level_with_good_decision() -> None:
    store, _, _ = _store_with_good_and_malformed()
    for level in (0, 1, 2):
        result = get_context(store, level)
        assert isinstance(result, GetContextResult)
        assert result.error is None
        assert result.content is not None
        assert "Adopt Redis cache" in result.content


# ── get_decision ──


def test_get_decision_good_header_and_full() -> None:
    store, _, good_body = _store_with_good_and_malformed()

    header = get_decision(store, 7, mode="header")
    assert header.error is None
    assert header.content is not None
    assert "# 007 — Adopt Redis cache" in header.content

    full = get_decision(store, 7, mode="full")
    assert full.error is None
    assert full.content == good_body


def test_get_decision_malformed_target_header_returns_typed_error() -> None:
    """A corrupt target in header mode returns a Result with a typed error —
    not an exception, and not a silent skip."""
    store, _, _ = _store_with_good_and_malformed()
    result = get_decision(store, _MALFORMED_NUM, mode="header")
    assert isinstance(result, GetDecisionResult)
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert str(_MALFORMED_NUM) in result.error.reason


def test_get_decision_malformed_target_full_returns_verbatim_body() -> None:
    """Full mode never parses, so a corrupt target still returns its body."""
    store, _, _ = _store_with_good_and_malformed()
    result = get_decision(store, _MALFORMED_NUM, mode="full")
    assert result.error is None
    assert result.content == _MALFORMED_BODY
