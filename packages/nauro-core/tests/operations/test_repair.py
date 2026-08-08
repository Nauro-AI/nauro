"""Tests for ``nauro_core.operations.repair`` — the pure repair planner.

Every fixture is built through ``format_decision`` so it cannot drift from the
on-disk v2 format. The load-bearing properties are that a plan is emitted only
for a single, wholly unambiguous orphan, that every other shape lands as a
typed refusal, and that the rewritten target survives a full round-trip
including frontmatter keys the reader does not model.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
    parse_decision,
)
from nauro_core.operations import InMemoryStore
from nauro_core.operations.repair import plan_supersede_repair


def _stem(num: int, slug: str = "decision") -> str:
    return f"{num:03d}-{slug}"


def _body(
    num: int,
    *,
    status: DecisionStatus = DecisionStatus.active,
    supersedes: str | None = None,
    superseded_by: str | None = None,
    version: int = 1,
    **extra: str,
) -> str:
    return format_decision(
        Decision(
            date=date(2026, 1, 1),
            confidence=DecisionConfidence.medium,
            status=status,
            version=version,
            supersedes=supersedes,
            superseded_by=superseded_by,
            num=num,
            title=f"Decision {num}",
            rationale=f"Rationale for decision {num}.",
            **extra,
        )
    )


def _orphan_store(**decisions: str) -> InMemoryStore:
    base = {
        _stem(20): _body(20, supersedes="19"),
        _stem(19): _body(19),
    }
    base.update(decisions)
    return InMemoryStore(decisions=base)


def _kinds(assessment) -> list[str]:
    return [refusal.kind for refusal in assessment.refusals]


# ── The single repairable shape ──


def test_plan_emitted_for_a_single_clean_orphan() -> None:
    assessment = plan_supersede_repair(_orphan_store())
    assert assessment.refusals == []
    plan = assessment.plan
    assert plan is not None
    assert (plan.child_num, plan.target_num) == (20, 19)
    assert plan.target_stem == _stem(19)
    assert plan.target_path == f"decisions/{_stem(19)}.md"
    assert plan.target_status is DecisionStatus.active
    assert plan.child_title == "Decision 20"
    assert plan.target_title == "Decision 19"
    assert plan.target_version == 1
    assert plan.target_date == date(2026, 1, 1)
    assert plan.child_date == date(2026, 1, 1)
    assert plan.pre_image_digest


def test_planned_content_flips_only_status_and_backref() -> None:
    store = _orphan_store()
    plan = plan_supersede_repair(store).plan
    assert plan is not None

    before = parse_decision(store.read_decision(_stem(19)) or "", f"{_stem(19)}.md")
    after = parse_decision(plan.new_content, f"{_stem(19)}.md")

    assert after.status is DecisionStatus.superseded
    assert after.superseded_by == "20"
    unchanged = {"status", "superseded_by", "content"}
    assert after.model_dump(exclude=unchanged) == before.model_dump(exclude=unchanged)


def test_planned_content_round_trips_through_the_formatter() -> None:
    plan = plan_supersede_repair(_orphan_store()).plan
    assert plan is not None
    reparsed = parse_decision(plan.new_content, f"{_stem(19)}.md")
    assert format_decision(reparsed) == plan.new_content


def test_unknown_frontmatter_keys_survive_the_rewrite() -> None:
    store = _orphan_store(**{_stem(19): _body(19, base_commit="deadbeef")})
    plan = plan_supersede_repair(store).plan
    assert plan is not None
    assert "base_commit: deadbeef" in plan.new_content
    assert parse_decision(plan.new_content, f"{_stem(19)}.md").model_extra == {
        "base_commit": "deadbeef"
    }


def test_no_orphan_yields_neither_plan_nor_refusal() -> None:
    store = InMemoryStore(
        decisions={
            _stem(20): _body(20, supersedes="19"),
            _stem(19): _body(19, status=DecisionStatus.superseded, superseded_by="20"),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert assessment.refusals == []
    assert assessment.orphans == ()


def test_healthy_chain_draws_no_refusals() -> None:
    # D30 retires D20, which already retired D10. Nothing is orphan-shaped, so
    # a settled chain must stay silent rather than report competing references.
    store = InMemoryStore(
        decisions={
            _stem(10): _body(10, status=DecisionStatus.superseded, superseded_by="20"),
            _stem(20): _body(
                20, status=DecisionStatus.superseded, superseded_by="30", supersedes="10"
            ),
            _stem(30): _body(30, supersedes="20"),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert assessment.refusals == []


# ── Refusals ──


def test_multiple_children_refuses() -> None:
    store = _orphan_store(**{_stem(21): _body(21, supersedes="19")})
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert len(assessment.orphans) == 2
    refusal = next(r for r in assessment.refusals if r.kind == "multiple_children")
    assert refusal.decisions == (19, 20, 21)
    assert "D19" in refusal.guidance


def test_cycle_member_refuses() -> None:
    store = InMemoryStore(
        decisions={
            _stem(20): _body(20, supersedes="19"),
            _stem(19): _body(19, supersedes="21"),
            _stem(21): _body(21, supersedes="19"),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert "cycle_member" in _kinds(assessment)


def test_unparseable_target_refuses() -> None:
    store = _orphan_store(**{_stem(19): "garbage that does not parse"})
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    refusal = next(r for r in assessment.refusals if r.kind == "unparseable_target")
    assert refusal.stems == (_stem(19),)


def test_unparseable_file_anywhere_refuses_as_a_possible_child() -> None:
    store = _orphan_store(**{_stem(40, "broken"): "garbage that does not parse"})
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    refusal = next(r for r in assessment.refusals if r.kind == "unparseable_child")
    assert refusal.stems == (_stem(40, "broken"),)
    assert refusal.decisions == ()


def test_duplicate_decision_number_refuses() -> None:
    store = InMemoryStore(
        decisions={
            _stem(20): _body(20, supersedes="19"),
            _stem(19, "first"): _body(19),
            _stem(19, "second"): _body(19),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    refusal = next(r for r in assessment.refusals if r.kind == "duplicate_decision_number")
    assert refusal.decisions == (19,)
    assert refusal.stems == (_stem(19, "first"), _stem(19, "second"))


def test_duplicate_child_number_refuses_and_names_the_number_once() -> None:
    # Two parseable files carry number 20 and the same forward edge. The
    # duplicate is the defect; the pair must not also render as two rival
    # children both spelled "D20".
    store = InMemoryStore(
        decisions={
            _stem(20, "first"): _body(20, supersedes="19"),
            _stem(20, "second"): _body(20, supersedes="19"),
            _stem(19): _body(19),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert assessment.orphans == ()
    assert _kinds(assessment) == ["duplicate_decision_number"]
    refusal = assessment.refusals[0]
    assert refusal.decisions == (20,)
    assert refusal.stems == (_stem(20, "first"), _stem(20, "second"))
    assert "D20, D20" not in refusal.guidance


def test_competing_reference_refuses_when_a_third_decision_names_the_target() -> None:
    # D18 records that D19 retired it. D19 is therefore already referenced by a
    # decision other than the child, so its rewrite is not unambiguous.
    store = _orphan_store(
        **{_stem(18): _body(18, status=DecisionStatus.superseded, superseded_by="19")}
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    refusal = next(r for r in assessment.refusals if r.kind == "competing_reference")
    assert refusal.decisions == (18, 19, 20)


def test_competing_reference_refuses_when_the_child_is_itself_superseded() -> None:
    store = _orphan_store(
        **{
            _stem(20): _body(
                20, status=DecisionStatus.superseded, superseded_by="21", supersedes="19"
            ),
            _stem(21): _body(21),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert "competing_reference" in _kinds(assessment)


def test_competing_reference_refuses_when_a_third_decision_claims_the_child() -> None:
    store = _orphan_store(**{_stem(22): _body(22, supersedes="20")})
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert "competing_reference" in _kinds(assessment)


def test_one_to_many_retirement_orphan_is_still_repairable() -> None:
    # D4 retires D2, D3 and D5; only D2 carries the reciprocal forward edge and
    # only D2's backref is missing. The other members reference the child, not
    # the target, so the pair stays unambiguous.
    store = InMemoryStore(
        decisions={
            _stem(4): _body(4, supersedes="2"),
            _stem(2): _body(2),
            _stem(3): _body(3, status=DecisionStatus.superseded, superseded_by="4"),
            _stem(5): _body(5, status=DecisionStatus.superseded, superseded_by="4"),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.refusals == []
    assert assessment.plan is not None
    assert (assessment.plan.child_num, assessment.plan.target_num) == (4, 2)


# ── Whole-store singleton rule ──


def test_two_independent_orphans_block_the_plan() -> None:
    store = _orphan_store(
        **{
            _stem(31): _body(31, supersedes="30"),
            _stem(30): _body(30),
        }
    )
    assessment = plan_supersede_repair(store)
    assert assessment.plan is None
    assert [(o.child, o.target) for o in assessment.orphans] == [(20, 19), (31, 30)]


def test_a_refusal_on_another_pair_does_not_block_the_only_orphan() -> None:
    # D41 is in a cycle with D42; the D20 -> D19 orphan is untouched by it.
    store = _orphan_store(
        **{
            _stem(41): _body(41, supersedes="42"),
            _stem(42): _body(42, supersedes="41"),
        }
    )
    assessment = plan_supersede_repair(store)
    assert "cycle_member" in _kinds(assessment)
    assert assessment.plan is not None
    assert (assessment.plan.child_num, assessment.plan.target_num) == (20, 19)


def test_refusals_are_sorted_and_deterministic() -> None:
    store = _orphan_store(
        **{
            _stem(21): _body(21, supersedes="19"),
            _stem(40, "broken"): "garbage that does not parse",
        }
    )
    first = plan_supersede_repair(store)
    second = plan_supersede_repair(store)
    assert first.model_dump() == second.model_dump()
    assert _kinds(first) == sorted(_kinds(first))


# ── Purity ──


def test_repair_module_touches_no_filesystem_or_clock() -> None:
    """The planner reads only through the Store protocol and takes no clock.

    A repair plan must be reproducible: the same store yields byte-identical
    content and the same digest, and the locked re-check in the CLI compares
    the two directly. Filesystem or clock access here would break that.
    """
    module = Path(plan_supersede_repair.__code__.co_filename)
    tree = ast.parse(module.read_text(encoding="utf-8"))
    forbidden = {"os", "pathlib", "io", "shutil", "tempfile", "time", "random", "subprocess"}
    imported: set[str] = set()
    clock_reads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Attribute) and node.attr in ("now", "today", "utcnow"):
            clock_reads.add(node.attr)
    assert imported & forbidden == set()
    assert clock_reads == set()
