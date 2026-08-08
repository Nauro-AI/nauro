"""Deterministic store-integrity diagnosis for ``nauro doctor``.

``diagnose_store`` reads a project store through the :class:`Store` protocol
and reports four kinds of *blocking* structural defect in the decision set:

    1. Unparseable decision files.
    2. Dangling supersession refs — a ``supersedes``/``superseded_by`` value
       pointing at a decision number with no file on disk.
    3. Supersession cycles — a directed cycle over the union of both ref
       directions, self-loops included.
    4. Status contradictions — an active decision carrying ``superseded_by``,
       or a forward/back conflict where ``X.supersedes=Y`` while ``Y`` records
       ``superseded_by=Z`` naming a third, present decision.

Alongside those it reports one *repairable* defect: a supersede backref orphan,
an active decision whose ``supersedes`` target is itself still active and
carries no reciprocal ``superseded_by``. Severity is split deliberately.
:attr:`StoreDiagnosis.is_clean` stays bound to the four blocking categories
above and is unmoved by an orphan, because an orphan is a recoverable
half-write rather than a store a reader cannot trust; it is surfaced through
:attr:`StoreDiagnosis.has_repairable_defects` instead, and ``nauro repair``
is the gated action that closes it.

It also surfaces unknown frontmatter keys — keys the reader tolerates and
preserves but does not model. This is advisory, not a defect: the tolerant
reader accepts them by design, so they do not make a store unclean.

Every check is zero-false-positive by construction and the output is fully
sorted, so two diagnoses over the same store are identical. The module stands
apart from ``graph.py``: that builder's edge collection keeps only live
endpoints and drops self-edges, which are exactly the anomalies this reports.

Pure: no I/O beyond the Store reads, no clock, no randomness. It is imported
submodule-only and is deliberately not part of ``nauro_core.__all__``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.operations.decision_lookup import scan_decisions
from nauro_core.operations.store import Store
from nauro_core.parsing import extract_decision_number

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

    Two shapes, discriminated by ``kind``:

    - ``active_with_superseded_by``: ``decision`` is active yet carries
      ``superseded_by=other``.
    - ``forward_back_conflict``: ``decision`` records ``supersedes=other``,
      but ``other`` records ``superseded_by=conflicting_with`` naming a third,
      present decision rather than ``decision``. ``conflicting_with`` is unset
      for the first shape.
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


class StoreDiagnosis(BaseModel):
    """The full result of :func:`diagnose_store`. Every list is sorted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unparseable: list[UnparseableDecision] = Field(default_factory=list)
    dangling_refs: list[DanglingRef] = Field(default_factory=list)
    cycles: list[SupersessionCycle] = Field(default_factory=list)
    contradictions: list[StatusContradiction] = Field(default_factory=list)
    supersede_orphans: list[SupersedeOrphan] = Field(default_factory=list)
    unknown_frontmatter_keys: list[UnknownFrontmatterKeys] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when no blocking defect was found.

        Bound to the four blocking categories only. Unknown frontmatter keys
        are advisory, and supersede backref orphans are repairable, so neither
        makes a store unclean.
        """
        return not (self.unparseable or self.dangling_refs or self.cycles or self.contradictions)

    @property
    def has_repairable_defects(self) -> bool:
        """True when the store carries a defect ``nauro repair`` can close."""
        return bool(self.supersede_orphans)


def diagnose_store(store: Store) -> StoreDiagnosis:
    """Diagnose store-integrity defects. Pure; reads only through ``store``."""
    parsed, failures = scan_decisions(store)

    # Existence is on-disk stems, not parsed nums: a present-but-unparseable
    # file still counts as existing, so a ref to it is reported once (as
    # unparseable) and never double-reported as dangling. The counts (not just
    # the set) are kept because the orphan check needs to know a number is
    # carried by exactly one file before it names that file repairable.
    stem_counts = Counter(
        num
        for stem in store.list_decisions()
        if (num := extract_decision_number(stem)) is not None
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
    supersede_orphans = _supersede_orphans(parsed, by_num, stem_counts, cycles)
    unknown_frontmatter_keys = _unknown_frontmatter_keys(parsed)

    return StoreDiagnosis(
        unparseable=unparseable,
        dangling_refs=dangling_refs,
        cycles=cycles,
        contradictions=contradictions,
        supersede_orphans=supersede_orphans,
        unknown_frontmatter_keys=unknown_frontmatter_keys,
    )


def _unknown_frontmatter_keys(parsed: list[Decision]) -> list[UnknownFrontmatterKeys]:
    """Decisions carrying tolerated-but-unmodeled frontmatter keys. Sorted."""
    rows: list[UnknownFrontmatterKeys] = []
    for d in parsed:
        extras = d.model_extra or {}
        if extras:
            rows.append(UnknownFrontmatterKeys(number=d.num, keys=tuple(sorted(extras))))
    return sorted(rows, key=lambda row: row.number)


def _index_by_num(parsed: list[Decision]) -> dict[int, Decision]:
    """Map each decision number to its parsed decision, first stem wins.

    Duplicate numbers are a separate anomaly this command does not report; the
    first occurrence in scan order is kept so target lookups are deterministic.
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
    """Detect directed cycles over the union of both ref directions.

    ``supersedes: Y`` on decision N yields the edge ``N -> Y``;
    ``superseded_by: X`` on decision N yields ``X -> N`` (X is newer, so X
    supersedes N). A reciprocal pair collapses to one directed edge and is not
    a cycle. Detection is by strongly connected component: an SCC of two or
    more nodes always contains a directed cycle, and a single node with a
    self-edge is a length-one cycle, so no acyclic chain can be flagged.
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

    Neighbors are visited in sorted order so traversal is deterministic; SCC
    membership is order-independent regardless, but a stable walk keeps the
    output reproducible.
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
    """Status/supersession contradictions. Sorted.

    Two shapes:

    (i) An active decision carrying a ``superseded_by`` value.

    (ii) A forward/back conflict anchored on the forward edge only: ``X``
    records ``supersedes=Y`` while ``Y`` records ``superseded_by=Z`` with ``Z``
    present on disk and ``Z != X``. Anchoring on the forward edge is the
    load-bearing guard for the one-to-many retirement convention (one forward
    root, other members back-only ``superseded_by``): a back-only member has no
    forward edge, so it is never examined and never flagged. Requiring ``Z``
    present keeps a dangling ``superseded_by`` reported once (as dangling)
    rather than also here.
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
    """Half-written supersessions: forward edge present, backref absent. Sorted.

    A row is emitted only when every one of these holds, so the finding names a
    single unambiguous pair of files and nothing else:

    - the child parses and is still active, and records ``supersedes=target``;
    - ``target`` is not the child's own number (a self-reference is a cycle);
    - exactly one on-disk stem carries each side's number, so both the file to
      repair and the decision claiming it are unambiguous. The child-side guard
      is what keeps one shape from reporting twice: two parseable files sharing
      a number and a forward edge are one duplicate-number defect, not two
      orphans;
    - the target's file parses, is still active, and carries no
      ``superseded_by``.

    A candidate whose child or target belongs to a reported cycle is suppressed:
    the cycle is the defect there, and every defect is reported once.
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
