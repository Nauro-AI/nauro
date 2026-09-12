"""The bounded tail walk behind ``get_context`` L0 and L1 and ``list_decisions``
must project exactly what the full scan projects, on a store built to break it:
duplicate numbers straddling a batch boundary, superseded runs, an unparseable
file, and a stem with no number that forces the full-scan fallback.
"""

from __future__ import annotations

from conftest import _seed_decision
from nauro_core.context import build_l0, build_l1
from nauro_core.decision_model import DecisionStatus
from nauro_core.operations import InMemoryStore, get_context, list_decisions
from nauro_core.operations.decision_lookup import (
    _TAIL_WALK_BATCH,
    _tail_batches,
    parse_all_decisions,
    parse_recent_active_decisions,
)
from nauro_core.operations.get_context import _load_context_files


class RecordingStore(InMemoryStore):
    """Counts the stems each bulk read asks for."""

    def __init__(self, decisions: dict[str, str]) -> None:
        super().__init__(decisions=decisions)
        self.read_batches: list[list[str]] = []

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        self.read_batches.append(list(stems))
        return super().read_decisions(stems)


def _adversarial_decisions(total: int) -> dict[str, str]:
    """``total`` numbered decisions where every fifth one is superseded, the number
    at the first batch boundary is carried by three stems, and one file is corrupt."""
    decisions: dict[str, str] = {}
    boundary = total - _TAIL_WALK_BATCH
    for num in range(1, total + 1):
        status = DecisionStatus.superseded if num % 5 == 0 else DecisionStatus.active
        stem, body = _seed_decision(num, f"Decision {num}", status=status)
        decisions[stem] = body
        if num == boundary:
            for suffix in ("aardvark", "zebra"):
                dup_stem, dup_body = _seed_decision(
                    num, f"Twin {suffix}", stem=f"{num:03d}-{suffix}"
                )
                decisions[dup_stem] = dup_body
    corrupt_stem, _ = _seed_decision(total - 2, "Corrupt", stem=f"{total - 2:03d}-corrupt")
    decisions[corrupt_stem] = "---\nnot: [valid\n---\n# broken\n"
    return decisions


def _reference_active(store: InMemoryStore) -> list:
    return [d for d in parse_all_decisions(store) if d.status is DecisionStatus.active]


def test_tail_batches_never_split_a_number_group() -> None:
    stems = sorted(_adversarial_decisions(80))
    batches = list(_tail_batches(stems))
    assert [s for batch in reversed(batches) for s in batch] == stems
    for earlier, later in zip(batches[1:], batches[:-1], strict=True):
        assert earlier[-1][:3] != later[0][:3]


def test_recent_active_matches_the_full_scan_tail_and_reads_less() -> None:
    store = RecordingStore(_adversarial_decisions(80))
    reference = _reference_active(store)
    store.read_batches.clear()
    recent = parse_recent_active_decisions(store, 10)
    assert len(recent) >= 10
    assert [d.num for d in recent] == [d.num for d in reference[-len(recent) :]]
    assert [d.title for d in recent] == [d.title for d in reference[-len(recent) :]]
    read = sum(len(batch) for batch in store.read_batches)
    assert read < len(store.list_decisions())


def test_recent_active_covers_the_boundary_group_when_the_walk_stops_on_it() -> None:
    total = 80
    store = RecordingStore(_adversarial_decisions(total))
    boundary = total - _TAIL_WALK_BATCH
    reference = _reference_active(store)
    count = sum(1 for d in reference if d.num > boundary)
    recent = parse_recent_active_decisions(store, count + 1)
    assert [d.num for d in recent] == [d.num for d in reference[-len(recent) :]]
    assert sum(1 for d in recent if d.num == boundary) == 3


def test_recent_active_with_more_than_the_store_holds_returns_all_active() -> None:
    store = RecordingStore(_adversarial_decisions(40))
    recent = parse_recent_active_decisions(store, 1000)
    assert [d.num for d in recent] == [d.num for d in _reference_active(store)]


def test_unnumbered_stem_falls_back_to_the_full_scan() -> None:
    decisions = _adversarial_decisions(60)
    _, body = _seed_decision(7, "Unnumbered carrier")
    decisions["notes-without-a-number"] = body
    store = RecordingStore(decisions)
    reference = _reference_active(store)
    store.read_batches.clear()
    recent = parse_recent_active_decisions(store, 10)
    assert [d.title for d in recent] == [d.title for d in reference]
    assert len(store.read_batches) == 1
    assert len(store.read_batches[0]) == len(store.list_decisions())


def test_context_levels_and_listing_match_the_full_scan() -> None:
    for total in (5, 33, 80, 130):
        store = InMemoryStore(decisions=_adversarial_decisions(total))
        full = parse_all_decisions(store)
        files = _load_context_files(store, 1)
        assert get_context(store, 0).content == build_l0(files, full)
        assert get_context(store, 1).content == build_l1(files, full)
        for limit in (0, 1, 7, 20, 500):
            expected = sorted(
                (d for d in full if d.status is DecisionStatus.active),
                key=lambda d: d.num,
                reverse=True,
            )[:limit]
            got = list_decisions(store, limit=limit).decisions
            assert [(r.number, r.title) for r in got] == [(d.num, d.title) for d in expected]
