"""Deterministic store-integrity diagnosis for ``nauro doctor``.

``diagnose_store`` reads a store through the :class:`Store` protocol and reports
six blocking defects: unparseable files, dangling refs, cycles, contradictions,
duplicate decision numbers, and duplicate normalized active titles.

Alongside those it reports one repairable defect, the supersede backref orphan,
and advisory unknown frontmatter keys. :attr:`StoreDiagnosis.is_clean` is bound
to the blocking six only: an orphan is a recoverable half-write surfaced
through :attr:`StoreDiagnosis.has_repairable_defects` and closed by the gated
``nauro repair``, and tolerated unknown keys are accepted by design.

Every check is zero-false-positive by construction and the output is fully
sorted, so two diagnoses over the same store are identical. Pure: no I/O beyond
the Store reads, no clock, no randomness. Imported submodule-only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.operations.decision_lookup import ScannedDecision, scan_decision_records
from nauro_core.operations.store import Store
from nauro_core.parsing import extract_decision_number
from nauro_core.validation import normalize_title

RefField = Literal["supersedes", "superseded_by"]


class UnparseableDecision(BaseModel):
    """A decision file that does not round-trip through the v2 parser."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stem: str
    error: str


class DanglingRef(BaseModel):
    """A supersession ref pointing at a decision number with no file on disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: int
    field: RefField
    target: int


class SupersessionCycle(BaseModel):
    """A directed cycle in the supersession graph.

    ``members`` is the sorted node set of the cycle: a single number for a
    self-loop, two or more for a longer cycle.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    members: tuple[int, ...]


class StatusContradiction(BaseModel):
    """A decision whose status and supersession fields disagree.

    ``active_with_superseded_by``: ``decision`` is active yet carries
    ``superseded_by=other``. ``forward_back_conflict``: ``decision`` records
    ``supersedes=other``, but ``other`` names a third decision in ``conflicting_with``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["active_with_superseded_by", "forward_back_conflict"]
    decision: int
    other: int
    conflicting_with: int | None = None


class SupersedeOrphan(BaseModel):
    """A half-written supersession: the forward edge exists, the backref does not.

    ``child`` is active and records ``supersedes=target``; ``target`` is still
    active and carries no ``superseded_by``. Repairable rather than blocking —
    see the module docstring for the severity split.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    child: int
    target: int


class UnknownFrontmatterKeys(BaseModel):
    """A parsed decision carrying frontmatter keys the reader does not model.

    Advisory only: the tolerant reader preserves these keys, so their presence
    is not an integrity defect. ``keys`` is sorted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    keys: tuple[str, ...]


class DuplicateDecisionNumber(BaseModel):
    """Every decision-file stem carrying one duplicated extracted number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    stems: tuple[str, ...]


class DecisionFileCarrier(BaseModel):
    """One parsed decision's number and on-disk carrier stem."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    stem: str


class DuplicateActiveTitle(BaseModel):
    """Parsed active decisions sharing one canonical normalized title."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_title: str
    carriers: tuple[DecisionFileCarrier, ...]


class StoreDiagnosis(BaseModel):
    """The full result of :func:`diagnose_store`. Every list is sorted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unparseable: list[UnparseableDecision] = Field(default_factory=list)
    dangling_refs: list[DanglingRef] = Field(default_factory=list)
    cycles: list[SupersessionCycle] = Field(default_factory=list)
    contradictions: list[StatusContradiction] = Field(default_factory=list)
    duplicate_numbers: list[DuplicateDecisionNumber] = Field(default_factory=list)
    duplicate_active_titles: list[DuplicateActiveTitle] = Field(default_factory=list)
    supersede_orphans: list[SupersedeOrphan] = Field(default_factory=list)
    unknown_frontmatter_keys: list[UnknownFrontmatterKeys] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when no blocking defect was found.

        Unknown frontmatter keys are advisory and orphans repairable, so neither counts.
        """
        return not (
            self.unparseable
            or self.dangling_refs
            or self.cycles
            or self.contradictions
            or self.duplicate_numbers
            or self.duplicate_active_titles
        )

    @property
    def has_repairable_defects(self) -> bool:
        """True when the store carries a defect ``nauro repair`` can close."""
        return bool(self.supersede_orphans)


def diagnose_store(store: Store) -> StoreDiagnosis:
    """Diagnose store-integrity defects. Pure; reads only through ``store``."""
    records, failures, stems = scan_decision_records(store)
    parsed = [record.decision for record in records]

    # Existence is on-disk stems, not parsed nums: a present-but-unparseable
    # file still counts as existing, so a ref to it is reported once (as
    # unparseable) and never double-reported as dangling. The counts (not just
    # the set) are kept because the orphan check needs to know a number is
    # carried by exactly one file before it names that file repairable.
    stem_counts = Counter(
        num for stem in stems if (num := extract_decision_number(stem)) is not None
    )
    existing_numbers = set(stem_counts)
    by_num = _index_by_num(parsed)

    unparseable = sorted(
        (UnparseableDecision(stem=f.stem, error=f.error) for f in failures),
        key=lambda row: row.stem,
    )
    dangling_refs = _dangling_refs(parsed, existing_numbers)
    cycles = _cycles(parsed)
    contradictions = _contradictions(parsed, by_num, existing_numbers)
    duplicate_numbers = _duplicate_numbers(stems)
    duplicate_active_titles = _duplicate_active_titles(records)
    supersede_orphans = _supersede_orphans(parsed, by_num, stem_counts, cycles)
    unknown_frontmatter_keys = _unknown_frontmatter_keys(parsed)

    return StoreDiagnosis(
        unparseable=unparseable,
        dangling_refs=dangling_refs,
        cycles=cycles,
        contradictions=contradictions,
        duplicate_numbers=duplicate_numbers,
        duplicate_active_titles=duplicate_active_titles,
        supersede_orphans=supersede_orphans,
        unknown_frontmatter_keys=unknown_frontmatter_keys,
    )


def _duplicate_numbers(stems: list[str]) -> list[DuplicateDecisionNumber]:
    """Group every carrier stem for each duplicated extracted number."""
    by_number: dict[int, list[str]] = {}
    for stem in stems:
        number = extract_decision_number(stem)
        if number is not None:
            by_number.setdefault(number, []).append(stem)
    return [
        DuplicateDecisionNumber(number=number, stems=tuple(sorted(carriers)))
        for number, carriers in sorted(by_number.items())
        if len(carriers) > 1
    ]


def _duplicate_active_titles(records: list[ScannedDecision]) -> list[DuplicateActiveTitle]:
    """Group parsed active carriers by the canonical normalized title."""
    by_title: dict[str, list[DecisionFileCarrier]] = {}
    for record in records:
        decision = record.decision
        if decision.status is not DecisionStatus.active:
            continue
        normalized_title = normalize_title(decision.title)
        by_title.setdefault(normalized_title, []).append(
            DecisionFileCarrier(number=decision.num, stem=record.stem)
        )
    return [
        DuplicateActiveTitle(
            normalized_title=normalized_title,
            carriers=tuple(sorted(carriers, key=lambda row: (row.number, row.stem))),
        )
        for normalized_title, carriers in sorted(by_title.items())
        if len(carriers) > 1
    ]


def _unknown_frontmatter_keys(parsed: list[Decision]) -> list[UnknownFrontmatterKeys]:
    """Decisions carrying tolerated-but-unmodeled frontmatter keys. Sorted."""
    rows: list[UnknownFrontmatterKeys] = []
    for d in parsed:
        extras = d.model_extra or {}
        if extras:
            rows.append(UnknownFrontmatterKeys(number=d.num, keys=tuple(sorted(extras))))
    return sorted(rows, key=lambda row: row.number)


def _index_by_num(parsed: list[Decision]) -> dict[int, Decision]:
    """Map each decision number to its parsed decision, first stem in scan order wins.

    Duplicate numbers are reported separately and never resolved here.
    """
    by_num: dict[int, Decision] = {}
    for d in parsed:
        by_num.setdefault(d.num, d)
    return by_num


def _dangling_refs(parsed: list[Decision], existing_numbers: set[int]) -> list[DanglingRef]:
    """Refs whose target number has no file on disk. Sorted."""
    rows: list[DanglingRef] = []
    for d in parsed:
        for field in ("supersedes", "superseded_by"):
            raw = getattr(d, field)
            if raw is None:
                continue
            target = int(raw)
            if target not in existing_numbers:
                rows.append(DanglingRef(source=d.num, field=field, target=target))
    return sorted(rows, key=lambda r: (r.source, r.field, r.target))


def _cycles(parsed: list[Decision]) -> list[SupersessionCycle]:
    """Detect directed cycles over both ref directions: ``supersedes: Y`` on N yields
    ``N -> Y``, ``superseded_by: X`` yields ``X -> N``. By strongly connected
    component, so a reciprocal pair is never a cycle and a self-edge always is.
    """
    adjacency: dict[int, set[int]] = {}
    for d in parsed:
        if d.supersedes is not None:
            adjacency.setdefault(d.num, set()).add(int(d.supersedes))
        if d.superseded_by is not None:
            adjacency.setdefault(int(d.superseded_by), set()).add(d.num)

    cycles: list[tuple[int, ...]] = []
    for scc in _strongly_connected_components(adjacency):
        if len(scc) > 1:
            cycles.append(tuple(sorted(scc)))
        else:
            (node,) = scc
            if node in adjacency.get(node, ()):
                cycles.append((node,))
    return [SupersessionCycle(members=members) for members in sorted(cycles)]


def _strongly_connected_components(adjacency: dict[int, set[int]]) -> list[list[int]]:
    """Tarjan's SCC, iterative to avoid recursion limits on long chains.

    Neighbors are visited in sorted order so the walk stays reproducible.
    """
    index_of: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    scc_stack: list[int] = []
    result: list[list[int]] = []
    counter = 0

    for root in sorted(adjacency):
        if root in index_of:
            continue
        # Each frame: [node, sorted neighbors, next-neighbor index].
        work: list[list] = [[root, sorted(adjacency.get(root, ())), 0]]
        index_of[root] = low[root] = counter
        counter += 1
        scc_stack.append(root)
        on_stack.add(root)
        while work:
            node, neighbors, i = work[-1]
            if i < len(neighbors):
                work[-1][2] += 1
                nbr = neighbors[i]
                if nbr not in index_of:
                    index_of[nbr] = low[nbr] = counter
                    counter += 1
                    scc_stack.append(nbr)
                    on_stack.add(nbr)
                    work.append([nbr, sorted(adjacency.get(nbr, ())), 0])
                elif nbr in on_stack:
                    low[node] = min(low[node], index_of[nbr])
            else:
                if low[node] == index_of[node]:
                    component: list[int] = []
                    while True:
                        member = scc_stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    result.append(component)
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
    return result


def _contradictions(
    parsed: list[Decision],
    by_num: dict[int, Decision],
    existing_numbers: set[int],
) -> list[StatusContradiction]:
    """Status contradictions, sorted: an active decision carrying ``superseded_by``, and
    a forward/back conflict where the target's ``superseded_by`` names a third present
    decision. Only forward edges are anchored, so back-only retirement members pass.
    """
    rows: list[StatusContradiction] = []
    for d in parsed:
        if d.status is DecisionStatus.active and d.superseded_by is not None:
            rows.append(
                StatusContradiction(
                    kind="active_with_superseded_by",
                    decision=d.num,
                    other=int(d.superseded_by),
                )
            )
        if d.supersedes is not None:
            target = by_num.get(int(d.supersedes))
            if target is not None and target.superseded_by is not None:
                claimed = int(target.superseded_by)
                if claimed in existing_numbers and claimed != d.num:
                    rows.append(
                        StatusContradiction(
                            kind="forward_back_conflict",
                            decision=d.num,
                            other=target.num,
                            conflicting_with=claimed,
                        )
                    )
    return sorted(
        rows,
        key=lambda r: (r.kind, r.decision, r.other, r.conflicting_with or 0),
    )


def _supersede_orphans(
    parsed: list[Decision],
    by_num: dict[int, Decision],
    stem_counts: Mapping[int, int],
    cycles: list[SupersessionCycle],
) -> list[SupersedeOrphan]:
    """Half-written supersessions, sorted: an active child records ``supersedes=target``
    and the target parses, is active, and carries no ``superseded_by``. Emitted only
    for one stem per number, no self-reference, and neither side in a reported cycle.
    """
    cycle_members = {member for cycle in cycles for member in cycle.members}
    rows: list[SupersedeOrphan] = []
    for d in parsed:
        if d.status is not DecisionStatus.active or d.supersedes is None:
            continue
        target_num = int(d.supersedes)
        if target_num == d.num:
            continue
        if stem_counts.get(d.num, 0) != 1 or stem_counts.get(target_num, 0) != 1:
            continue
        if d.num in cycle_members or target_num in cycle_members:
            continue
        target = by_num.get(target_num)
        if target is None:
            continue
        if target.status is not DecisionStatus.active or target.superseded_by is not None:
            continue
        rows.append(SupersedeOrphan(child=d.num, target=target_num))
    return sorted(rows, key=lambda row: (row.child, row.target))
