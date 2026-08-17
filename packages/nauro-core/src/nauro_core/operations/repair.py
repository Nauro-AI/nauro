"""Plan the one supersession repair a machine may make on a human's word.

A supersede backref orphan is a half-written supersession: the newer decision
records ``supersedes=<old>`` but the old one was never flipped, so it still reads
active with no ``superseded_by``. ``nauro doctor`` names the shape; this module
decides whether it can close without judgment and renders the bytes that close it.

The bar is narrow: a plan exists only when the store holds exactly one orphan and
nothing else references either side. Every other shape returns a typed
:class:`RepairRefusal` with guidance and no plan. No refusal covers a target that
moved since the child was filed, so the prompt shows both sides' version and date.

Pure: reads through :class:`Store` only, no clock, no randomness, writes nothing.
The caller re-plans under the write lock and compares
:attr:`RepairPlan.pre_image_digest` before writing.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from nauro_core.decision_model import Decision, DecisionStatus, format_decision
from nauro_core.doctor import SupersedeOrphan, SupersessionCycle, diagnose_store
from nauro_core.operations.decision_lookup import scan_decisions
from nauro_core.operations.store import Store
from nauro_core.parsing import _decision_path, extract_decision_number

RepairRefusalKind = Literal[
    "multiple_children",
    "cycle_member",
    "unparseable_child",
    "unparseable_target",
    "duplicate_decision_number",
    "competing_reference",
]


class RepairRefusal(BaseModel):
    """One shape the planner will not touch, with the reason already worded.

    ``decisions`` names every implicated decision number and ``stems`` every file,
    both sorted; an empty ``decisions`` marks a store-wide refusal that blocks every
    candidate. ``guidance`` is the sentence a surface prints verbatim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RepairRefusalKind
    decisions: tuple[int, ...] = ()
    stems: tuple[str, ...] = ()
    guidance: str

    def blocks(self, orphan: SupersedeOrphan) -> bool:
        """True when this refusal makes ``orphan`` unsafe to repair."""
        if not self.decisions:
            return True
        return orphan.child in self.decisions or orphan.target in self.decisions


class RepairPlan(BaseModel):
    """The exact rewrite that would close one orphan, and the facts to judge it.

    ``new_content`` is the target's file after the flip. ``pre_image_digest`` is the
    sha256 of its current bytes, so a caller holding the write lock can confirm
    nothing moved. The versions, dates and target status ride along for the prompt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    child_num: int
    child_title: str
    child_version: int
    child_date: date
    target_num: int
    target_title: str
    target_version: int
    target_date: date
    target_status: DecisionStatus
    target_stem: str
    target_path: str
    new_content: str
    pre_image_digest: str


class RepairAssessment(BaseModel):
    """What the planner found: at most one plan, plus everything it refused.

    ``orphans`` is the full detected set, carried so a surface can explain a
    missing plan when more than one orphan exists — the singleton rule is a
    property of the store, not of any single candidate, so no refusal names it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: RepairPlan | None = None
    refusals: list[RepairRefusal] = Field(default_factory=list)
    orphans: tuple[SupersedeOrphan, ...] = ()


class _Candidate(NamedTuple):
    """A forward supersession edge whose backref is not already in place."""

    child: int
    target: int


def plan_supersede_repair(store: Store) -> RepairAssessment:
    """Assess the store and plan the single repairable orphan, if there is one."""
    diagnosis = diagnose_store(store)
    parsed, failures = scan_decisions(store)

    by_num: dict[int, Decision] = {}
    for decision in parsed:
        by_num.setdefault(decision.num, decision)

    stems_by_num: dict[int, list[str]] = {}
    for stem in store.list_decisions():
        num = extract_decision_number(stem)
        if num is not None:
            stems_by_num.setdefault(num, []).append(stem)
    stem_counts = Counter({num: len(stems) for num, stems in stems_by_num.items()})
    failed_stems = {failure.stem: failure.error for failure in failures}

    candidates = _candidates(parsed, by_num, stem_counts)
    refusals = _refusals(
        candidates=candidates,
        parsed=parsed,
        cycles=diagnosis.cycles,
        stems_by_num=stems_by_num,
        stem_counts=stem_counts,
        failed_stems=failed_stems,
    )

    orphans = tuple(diagnosis.supersede_orphans)
    plan: RepairPlan | None = None
    if len(orphans) == 1 and not any(refusal.blocks(orphans[0]) for refusal in refusals):
        plan = _build_plan(orphans[0], by_num, stems_by_num)

    return RepairAssessment(plan=plan, refusals=refusals, orphans=orphans)


def _candidates(
    parsed: list[Decision],
    by_num: dict[int, Decision],
    stem_counts: Counter[int],
) -> list[_Candidate]:
    """Forward supersession edges that still need a backref written, sorted and
    deduplicated by number pair. A settled pair is not a candidate, so a healthy
    store yields none; an edge whose target has no file is a dangling ref, not one.
    """
    candidates: set[_Candidate] = set()
    for decision in parsed:
        if decision.supersedes is None:
            continue
        target_num = int(decision.supersedes)
        if target_num == decision.num or stem_counts[target_num] == 0:
            continue
        target = by_num.get(target_num)
        if target is not None and target.superseded_by == str(decision.num):
            continue
        candidates.add(_Candidate(child=decision.num, target=target_num))
    return sorted(candidates)


def _refusals(
    *,
    candidates: list[_Candidate],
    parsed: list[Decision],
    cycles: list[SupersessionCycle],
    stems_by_num: dict[int, list[str]],
    stem_counts: Counter[int],
    failed_stems: dict[str, str],
) -> list[RepairRefusal]:
    """Every reason the planner will not act, deduplicated and sorted."""
    if not candidates:
        return []

    by_num: dict[int, Decision] = {}
    for decision in parsed:
        by_num.setdefault(decision.num, decision)
    cycle_by_member = {member: cycle for cycle in cycles for member in cycle.members}
    children_by_target: dict[int, list[int]] = {}
    for candidate in candidates:
        children_by_target.setdefault(candidate.target, []).append(candidate.child)

    rows: dict[tuple, RepairRefusal] = {}

    def add(refusal: RepairRefusal) -> None:
        rows.setdefault((refusal.kind, refusal.decisions, refusal.stems), refusal)

    for candidate in candidates:
        for num in (candidate.child, candidate.target):
            if stem_counts[num] > 1:
                add(_duplicate_number_refusal(num, stems_by_num[num]))
            cycle = cycle_by_member.get(num)
            if cycle is not None:
                add(_cycle_refusal(cycle))

        target_stems = stems_by_num[candidate.target]
        if len(target_stems) == 1 and target_stems[0] in failed_stems:
            add(_unparseable_target_refusal(candidate, target_stems[0]))

        siblings = children_by_target[candidate.target]
        if len(siblings) > 1:
            add(_multiple_children_refusal(candidate.target, siblings))

        competing = _competing_references(candidate, parsed, by_num)
        if competing:
            add(_competing_reference_refusal(candidate, competing))

    # A file nobody could read might carry another forward edge, so no
    # single-child claim can be certified while one exists. Files already named
    # as an unparseable target are left to that refusal so each is reported once.
    named_targets = {
        stems_by_num[candidate.target][0]
        for candidate in candidates
        if len(stems_by_num[candidate.target]) == 1
    }
    unread = sorted(stem for stem in failed_stems if stem not in named_targets)
    if unread:
        add(_unparseable_child_refusal(unread))

    return sorted(rows.values(), key=lambda row: (row.kind, row.decisions, row.stems))


def _competing_references(
    candidate: _Candidate,
    parsed: list[Decision],
    by_num: dict[int, Decision],
) -> tuple[int, ...]:
    """Decisions whose own refs compete with the backref that would be written: another
    decision already records ``superseded_by`` naming the target, or the child is
    itself retired or claimed, reported then as the child's own number.
    """
    competing: set[int] = set()
    for decision in parsed:
        if (
            decision.num != candidate.child
            and decision.superseded_by is not None
            and int(decision.superseded_by) == candidate.target
        ):
            competing.add(decision.num)
        if (
            decision.num != candidate.target
            and decision.supersedes is not None
            and int(decision.supersedes) == candidate.child
        ):
            competing.add(decision.num)
    child = by_num.get(candidate.child)
    if child is not None and (
        child.status is DecisionStatus.superseded or child.superseded_by is not None
    ):
        competing.add(child.num)
    return tuple(sorted(competing))


def _build_plan(
    orphan: SupersedeOrphan,
    by_num: dict[int, Decision],
    stems_by_num: dict[int, list[str]],
) -> RepairPlan:
    """Render the flipped target for a candidate already cleared of refusals. Only the
    two frontmatter fields move, through the same ``model_copy`` plus
    ``format_decision`` path a supersede write takes, so the bytes match exactly.
    """
    child = by_num[orphan.child]
    target = by_num[orphan.target]
    target_stem = stems_by_num[orphan.target][0]
    flipped = target.model_copy(
        update={
            "status": DecisionStatus.superseded,
            "superseded_by": str(orphan.child),
        }
    )
    return RepairPlan(
        child_num=child.num,
        child_title=child.title,
        child_version=child.version,
        child_date=child.date,
        target_num=target.num,
        target_title=target.title,
        target_version=target.version,
        target_date=target.date,
        target_status=target.status,
        target_stem=target_stem,
        target_path=_decision_path(target_stem),
        new_content=format_decision(flipped),
        pre_image_digest=hashlib.sha256(target.content.encode("utf-8")).hexdigest(),
    )


# ── Refusal constructors (one per kind, guidance worded once) ──


def _label(num: int) -> str:
    return f"D{num}"


def _join(nums: tuple[int, ...] | list[int]) -> str:
    return ", ".join(_label(num) for num in nums)


def _duplicate_number_refusal(num: int, stems: list[str]) -> RepairRefusal:
    return RepairRefusal(
        kind="duplicate_decision_number",
        decisions=(num,),
        stems=tuple(sorted(stems)),
        guidance=(
            f"{_label(num)} is carried by more than one file "
            f"({', '.join(sorted(stems))}); renumber or remove the duplicate "
            "before repairing."
        ),
    )


def _cycle_refusal(cycle: SupersessionCycle) -> RepairRefusal:
    if len(cycle.members) == 1:
        detail = f"{_label(cycle.members[0])} references itself"
    else:
        detail = f"{_join(cycle.members)} form a supersession cycle"
    return RepairRefusal(
        kind="cycle_member",
        decisions=cycle.members,
        guidance=f"{detail}; untangle it by hand before repairing.",
    )


def _unparseable_target_refusal(candidate: _Candidate, stem: str) -> RepairRefusal:
    return RepairRefusal(
        kind="unparseable_target",
        decisions=(candidate.target, candidate.child),
        stems=(stem,),
        guidance=(
            f"{_label(candidate.target)} ({stem}) does not parse, so its "
            "frontmatter cannot be rewritten safely; fix the file first."
        ),
    )


def _unparseable_child_refusal(stems: list[str]) -> RepairRefusal:
    return RepairRefusal(
        kind="unparseable_child",
        stems=tuple(stems),
        guidance=(
            f"{', '.join(stems)} does not parse, so the decisions that claim to "
            "supersede others cannot be fully enumerated; fix the file first."
        ),
    )


def _multiple_children_refusal(target: int, children: list[int]) -> RepairRefusal:
    return RepairRefusal(
        kind="multiple_children",
        decisions=tuple(sorted({target, *children})),
        guidance=(
            f"{_join(sorted(children))} all claim to supersede "
            f"{_label(target)}; only a human can say which one retires it."
        ),
    )


def _competing_reference_refusal(
    candidate: _Candidate,
    competing: tuple[int, ...],
) -> RepairRefusal:
    return RepairRefusal(
        kind="competing_reference",
        decisions=tuple(sorted({candidate.child, candidate.target, *competing})),
        guidance=(
            f"{_join(competing)} already reference "
            f"{_label(candidate.child)} or {_label(candidate.target)}; the pair "
            "is not isolated enough to rewrite automatically."
        ),
    )


__all__ = [
    "RepairAssessment",
    "RepairPlan",
    "RepairRefusal",
    "RepairRefusalKind",
    "plan_supersede_repair",
]
