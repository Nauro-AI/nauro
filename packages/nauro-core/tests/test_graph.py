"""Tests for nauro_core.graph — the pure decision-graph payload builder.

Builder invariants only; no I/O. Decision fixtures are constructed in memory
and open-question fixtures are parsed from markdown strings, so these never
read the live store.
"""

import json
from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    DecisionType,
)
from nauro_core.graph import GRAPH_PAYLOAD_VERSION, build_graph_payload
from nauro_core.questions import OpenQuestionsFile


def make_decision(
    num,
    title=None,
    status="active",
    supersedes=None,
    superseded_by=None,
    decision_type=None,
    confidence="medium",
    body="",
    day=1,
):
    """Construct an in-memory Decision for graph fixtures.

    A superseded status carries a superseded_by ref by default so the model's
    superseded-requires-ref validator is satisfied without the caller spelling
    it out on every node.
    """
    status_enum = DecisionStatus(status)
    if status_enum is DecisionStatus.superseded and superseded_by is None:
        superseded_by = "9999"
    return Decision(
        date=date(2026, 4, day),
        confidence=DecisionConfidence(confidence),
        status=status_enum,
        decision_type=DecisionType(decision_type) if decision_type is not None else None,
        supersedes=supersedes,
        superseded_by=superseded_by,
        num=num,
        title=title if title is not None else f"Decision {num}",
        rationale="A rationale long enough to be plausible decision text.",
        body=body,
    )


# ── Shared fixtures (module-level functions, not test-class instances) ──


def fan_in_13():
    """A 13-way fan-in: node 100 retires a cluster via one forward edge to 7
    plus 12 back-only superseded_by refs (8..19). Exercises forward-plus-back
    union inside one fan."""
    retiring = make_decision(100, title="Consolidate the cluster", supersedes="7")
    forward_target = make_decision(7, status="superseded", superseded_by="100")
    back_only = [make_decision(n, status="superseded", superseded_by="100") for n in range(8, 20)]
    return [retiring, forward_target, *back_only]


def mixed_fan():
    """A fan centered on 40 (forward supersedes 26, back-only refs 27/28) plus a
    linear tail 40 -> 50 -> 60 where 60 itself branches by also superseding 55.
    One connected component containing a fan and a branching tail."""
    return [
        make_decision(40, supersedes="26"),
        make_decision(26, status="superseded", superseded_by="40"),
        make_decision(27, status="superseded", superseded_by="40"),
        make_decision(28, status="superseded", superseded_by="40"),
        make_decision(50, supersedes="40", status="superseded", superseded_by="60"),
        make_decision(60, supersedes="50"),
        make_decision(55, status="superseded", superseded_by="60"),
    ]


def assert_edges_partition_components(payload):
    """Every supersession edge appears in exactly one component, and the union
    of component edges equals the payload's edge list."""
    component_edges = [(e["from"], e["to"]) for c in payload["components"] for e in c["edges"]]
    all_edges = [(e["from"], e["to"]) for e in payload["supersession_edges"]]
    assert sorted(component_edges) == sorted(all_edges)
    assert len(component_edges) == len(set(component_edges))


# ── Scaffold seed ──


class TestScaffoldSeed:
    def test_scaffold_seed_excluded(self):
        decisions = [
            make_decision(1, title="Initial project setup"),
            make_decision(2, title="Use Postgres"),
        ]
        payload = build_graph_payload(decisions)
        assert [n["number"] for n in payload["nodes"]] == [2]
        assert payload["decision_count"] == 1

    def test_real_d1_with_different_title_retained(self):
        decisions = [
            make_decision(1, title="Adopt event sourcing"),
            make_decision(2, title="Use Postgres"),
        ]
        payload = build_graph_payload(decisions)
        assert [n["number"] for n in payload["nodes"]] == [1, 2]

    def test_scaffold_seed_contributes_no_edges_or_citations(self):
        # A real D1 supersedes 2 and cites D2 in its body; the scaffold seed,
        # present alongside under the same number, must contribute nothing.
        decisions = [
            make_decision(1, title="Initial project setup", body="see D2"),
            make_decision(1, title="Adopt event sourcing", supersedes="2", body="replaces D2"),
            make_decision(2, status="superseded", superseded_by="1"),
        ]
        payload = build_graph_payload(decisions)
        # The seed is dropped before dedup, so the kept D1 is the real one and
        # is NOT recorded as a duplicate of the seed.
        assert [n["number"] for n in payload["nodes"]] == [1, 2]
        kept_one = next(n for n in payload["nodes"] if n["number"] == 1)
        assert kept_one["title"] == "Adopt event sourcing"
        assert payload["supersession_edges"] == [{"from": 1, "to": 2}]
        # The body citation D2 coincides with the supersession pair, so excluded.
        assert payload["citation_edges"] == []
        assert payload["stats"]["duplicate_numbers"] == []


# ── Node shape ──


class TestNodes:
    def test_node_fields(self):
        d = make_decision(
            5,
            title="Pick a queue",
            status="superseded",
            decision_type="infrastructure",
            confidence="high",
            day=9,
        )
        payload = build_graph_payload([d], project="demo")
        assert payload["nodes"][0] == {
            "number": 5,
            "title": "Pick a queue",
            "status": "superseded",
            "decision_type": "infrastructure",
            "confidence": "high",
            "date": "2026-04-09",
        }
        assert payload["project"] == "demo"

    def test_null_decision_type(self):
        payload = build_graph_payload([make_decision(3, decision_type=None)])
        assert payload["nodes"][0]["decision_type"] is None

    def test_nodes_sorted_ascending(self):
        payload = build_graph_payload([make_decision(7), make_decision(2), make_decision(5)])
        assert [n["number"] for n in payload["nodes"]] == [2, 5, 7]

    def test_max_decision_number(self):
        payload = build_graph_payload([make_decision(2), make_decision(9), make_decision(4)])
        assert payload["max_decision_number"] == 9


# ── Input not mutated ──


class TestInputNotMutated:
    def test_input_list_and_decisions_unchanged(self):
        decisions = mixed_fan()
        before_len = len(decisions)
        before_ids = [id(d) for d in decisions]
        before_repr = [d.model_dump() for d in decisions]
        build_graph_payload(decisions)
        assert len(decisions) == before_len
        assert [id(d) for d in decisions] == before_ids
        assert [d.model_dump() for d in decisions] == before_repr


# ── Supersession edges: union of both directions ──


class TestSupersessionEdgeUnion:
    def test_forward_and_back_dedup_to_one(self):
        decisions = [
            make_decision(2, supersedes="1"),
            make_decision(1, status="superseded", superseded_by="2"),
        ]
        payload = build_graph_payload(decisions)
        # Forward (supersedes) and back (superseded_by) describe the same edge;
        # the union dedups it to exactly one.
        assert payload["supersession_edges"] == [{"from": 2, "to": 1}]

    def test_back_only_superseded_by_emits_edge(self):
        # B has no `supersedes: A`; only A carries `superseded_by: B`.
        decisions = [
            make_decision(1, status="superseded", superseded_by="2"),
            make_decision(2),
        ]
        payload = build_graph_payload(decisions)
        assert payload["supersession_edges"] == [{"from": 2, "to": 1}]

    def test_edge_to_missing_node_dropped(self):
        # supersedes points at a decision not present (e.g. the scaffold seed).
        decisions = [make_decision(2, supersedes="1", title="Real")]
        payload = build_graph_payload(decisions)
        assert payload["supersession_edges"] == []


# ── Components: branching topologies ──


class TestComponentsFanIn:
    def test_single_component(self):
        payload = build_graph_payload(fan_in_13())
        assert payload["stats"]["component_count"] == 1
        assert payload["components"][0]["nodes"] == [7, *range(8, 20), 100]

    def test_all_13_edges_present_once(self):
        payload = build_graph_payload(fan_in_13())
        comp = payload["components"][0]
        expected = [{"from": 100, "to": t} for t in [7, *range(8, 20)]]
        assert comp["edges"] == expected
        assert len(comp["edges"]) == 13
        assert payload["stats"]["supersession_edge_count"] == 13

    def test_fan_in_node_is_branch_point(self):
        payload = build_graph_payload(fan_in_13())
        assert payload["components"][0]["branch_points"] == [100]
        assert payload["stats"]["branch_point_count"] == 1

    def test_edges_partition(self):
        assert_edges_partition_components(build_graph_payload(fan_in_13()))


class TestComponentsMixedFanBranchingTail:
    def test_one_component(self):
        payload = build_graph_payload(mixed_fan())
        assert payload["stats"]["component_count"] == 1
        assert payload["components"][0]["nodes"] == [26, 27, 28, 40, 50, 55, 60]

    def test_edges_sorted_and_complete(self):
        payload = build_graph_payload(mixed_fan())
        assert payload["components"][0]["edges"] == [
            {"from": 40, "to": 26},
            {"from": 40, "to": 27},
            {"from": 40, "to": 28},
            {"from": 50, "to": 40},
            {"from": 60, "to": 50},
            {"from": 60, "to": 55},
        ]

    def test_branch_points_explicit(self):
        payload = build_graph_payload(mixed_fan())
        # 40 fans out to 26/27/28; 60 fans out to 50/55. The linear node 50 is
        # not a branch point.
        assert payload["components"][0]["branch_points"] == [40, 60]

    def test_edges_partition(self):
        assert_edges_partition_components(build_graph_payload(mixed_fan()))


class TestComponentOrdering:
    def test_sorted_by_size_then_smallest_number(self):
        decisions = [
            make_decision(6, supersedes="5"),
            make_decision(5, status="superseded", superseded_by="6"),
            make_decision(11, supersedes="10"),
            make_decision(12, supersedes="11"),
            make_decision(10, status="superseded", superseded_by="11"),
        ]
        payload = build_graph_payload(decisions)
        assert [len(c["nodes"]) for c in payload["components"]] == [3, 2]
        assert payload["components"][0]["nodes"] == [10, 11, 12]
        assert payload["components"][1]["nodes"] == [5, 6]

    def test_equal_size_tiebreak_on_smallest_member(self):
        decisions = [
            make_decision(6, supersedes="5"),
            make_decision(5, status="superseded", superseded_by="6"),
            make_decision(3, supersedes="2"),
            make_decision(2, status="superseded", superseded_by="3"),
        ]
        payload = build_graph_payload(decisions)
        assert [c["nodes"][0] for c in payload["components"]] == [2, 5]


# ── Determinism ──


class TestDeterminism:
    def test_build_twice_byte_identical(self):
        decisions = mixed_fan()
        first = json.dumps(build_graph_payload(decisions))
        second = json.dumps(build_graph_payload(decisions))
        assert first == second

    def test_input_order_does_not_change_output(self):
        decisions = fan_in_13()
        forward = build_graph_payload(decisions)
        reversed_in = build_graph_payload(list(reversed(decisions)))
        assert forward == reversed_in


# ── Citation scan grammar ──


class TestCitationScan:
    def test_plain_d_form(self):
        decisions = [
            make_decision(10, body="As noted in D7 we keep the queue."),
            make_decision(7),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == [{"from": 10, "to": 7}]

    def test_zero_padded_form(self):
        decisions = [
            make_decision(10, body="See D007 for the rationale."),
            make_decision(7),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == [{"from": 10, "to": 7}]

    def test_decision_hyphen_form(self):
        decisions = [
            make_decision(10, body="Superseded per decision-7 last quarter."),
            make_decision(7),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == [{"from": 10, "to": 7}]

    def test_prefix_collision_only_d1_inside_d118(self):
        # The body's ONLY "D1" occurrence is inside "D118". A broken substring
        # scanner that matched the prefix would fabricate a 200->1 edge; reading
        # the full digit run yields 118 only.
        decisions = [
            make_decision(200, body="Compare against D118 before deciding."),
            make_decision(1, title="Real first decision"),
            make_decision(118),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == [{"from": 200, "to": 118}]

    def test_letter_preceded_token_not_a_citation(self):
        # A "keyID70" token must not yield a citation to D70.
        decisions = [
            make_decision(200, body="The keyID70 identifier is unrelated."),
            make_decision(70),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == []

    def test_uuid_substring_not_a_citation(self):
        # A UUID4 body must not fabricate a citation (the live phantom-edge case
        # where "...d4..." yielded a D4 edge).
        decisions = [
            make_decision(200, body="ref 7c9e6679-7425-40de-944b-e07fc1f90ae7 only"),
            make_decision(4),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == []

    def test_unicode_digit_does_not_crash(self):
        # A superscript footnote after the number must not reach int() and raise.
        decisions = [
            make_decision(200, body="See D118¹ in the footnote."),
            make_decision(118),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == [{"from": 200, "to": 118}]

    def test_self_reference_excluded(self):
        decisions = [make_decision(7, body="This is D7 talking about itself.")]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == []

    def test_out_of_range_above_max_excluded(self):
        decisions = [
            make_decision(10, body="A forward pointer to D999 that does not exist."),
            make_decision(7),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == []

    def test_citation_to_missing_node_excluded(self):
        # 8 is in range (< max 10) but no node 8 exists.
        decisions = [
            make_decision(10, body="See D8 for context."),
            make_decision(7),
        ]
        payload = build_graph_payload(decisions)
        assert payload["citation_edges"] == []

    def test_supersession_pair_excluded_same_direction(self):
        # 10 supersedes 7 AND cites D7; the forward pair is already a
        # supersession edge, so it is not also a citation edge.
        decisions = [
            make_decision(10, supersedes="7", body="Replaces D7 entirely."),
            make_decision(7, status="superseded", superseded_by="10"),
        ]
        payload = build_graph_payload(decisions)
        assert payload["supersession_edges"] == [{"from": 10, "to": 7}]
        assert payload["citation_edges"] == []

    def test_supersession_pair_excluded_unordered(self):
        # 70 supersedes 69 (edge 70->69). 69's body cites D70, which would be a
        # citation 69->70 mirroring the supersession in the opposite direction.
        # The exclusion is on the unordered pair, so no citation edge is emitted.
        decisions = [
            make_decision(70, supersedes="69"),
            make_decision(
                69,
                status="superseded",
                superseded_by="70",
                body="Replaced by D70, see there.",
            ),
        ]
        payload = build_graph_payload(decisions)
        assert payload["supersession_edges"] == [{"from": 70, "to": 69}]
        assert payload["citation_edges"] == []


# ── Open-questions filter ──


class TestOpenQuestionsFilter:
    def test_unresolved_only_resolved_interleaved_excluded(self):
        content = (
            "# Open Questions\n"
            "- [Q1] First open question?\n"
            "- [Resolved by D5 on 2026-04-02] [Q2] Already settled question?\n"
            "- [Q3] Third open question?\n"
        )
        questions = OpenQuestionsFile.parse(content)
        payload = build_graph_payload([], questions=questions)
        assert [q["id"] for q in payload["open_questions"]] == ["Q1", "Q3"]

    def test_body_capped_to_first_sentence(self):
        content = (
            "# Open Questions\n- [Q1] First sentence here. Second sentence should be dropped.\n"
        )
        questions = OpenQuestionsFile.parse(content)
        payload = build_graph_payload([], questions=questions)
        assert payload["open_questions"][0]["body"] == "First sentence here."

    def test_body_abbreviation_not_clipped(self):
        content = "# Open Questions\n- [Q1] Should we e.g. cache the index here?\n"
        questions = OpenQuestionsFile.parse(content)
        payload = build_graph_payload([], questions=questions)
        assert payload["open_questions"][0]["body"] == "Should we e.g. cache the index here?"

    def test_no_questions_yields_empty(self):
        payload = build_graph_payload([], questions=None)
        assert payload["open_questions"] == []


# ── Cycle guard ──


class TestCycleGuard:
    def test_back_reference_does_not_infinite_loop(self):
        # A and B each reference the other; the union produces both directed
        # edges. Traversal must terminate and the edges land in one component.
        decisions = [
            make_decision(1, supersedes="2", superseded_by="2", status="superseded"),
            make_decision(2, supersedes="1", superseded_by="1", status="superseded"),
        ]
        payload = build_graph_payload(decisions)
        assert payload["stats"]["component_count"] == 1
        comp = payload["components"][0]
        assert comp["nodes"] == [1, 2]
        assert comp["edges"] == [{"from": 1, "to": 2}, {"from": 2, "to": 1}]
        assert_edges_partition_components(payload)


# ── Duplicate decision numbers ──


class TestDuplicateNumbers:
    def test_first_kept_duplicate_recorded(self):
        # Two files resolve to number 5; the deterministic key keeps "Alpha".
        decisions = [
            make_decision(5, title="Beta version"),
            make_decision(5, title="Alpha version"),
            make_decision(6, title="Other"),
        ]
        payload = build_graph_payload(decisions)
        assert [n["number"] for n in payload["nodes"]] == [5, 6]
        kept = next(n for n in payload["nodes"] if n["number"] == 5)
        assert kept["title"] == "Alpha version"
        assert payload["stats"]["duplicate_numbers"] == [5]
        assert payload["decision_count"] == 2

    def test_identical_title_duplicate_input_order_invariant(self):
        # Same number, same title; the duplicates differ only on date. The total
        # ordering key keeps resolution input-order independent.
        a = make_decision(5, title="Same", day=1)
        b = make_decision(5, title="Same", day=9)
        payload_ab = build_graph_payload([a, b])
        payload_ba = build_graph_payload([b, a])
        assert payload_ab == payload_ba
        # The earlier date sorts first and is kept.
        assert payload_ab["nodes"][0]["date"] == "2026-04-01"
        assert payload_ab["stats"]["duplicate_numbers"] == [5]

    def test_dropped_duplicate_contributes_no_edge_or_citation(self):
        # The dropped file for number 5 carries supersedes + a body reference;
        # neither must appear, because only the kept decision contributes.
        kept = make_decision(5, title="Alpha", day=1)
        dropped = make_decision(5, title="Beta", day=9, supersedes="6", body="see D6")
        target = make_decision(6, status="superseded", superseded_by="5")
        payload = build_graph_payload([kept, dropped, target])
        assert payload["stats"]["duplicate_numbers"] == [5]
        # Node 6's superseded_by still points at 5, so that back-only edge is
        # legitimate and present. The dropped file's supersedes adds nothing new.
        assert payload["supersession_edges"] == [{"from": 5, "to": 6}]
        # The dropped file's body "see D6" must not fabricate a citation; the
        # surviving 5->6 relationship is a supersession pair, so excluded anyway.
        assert payload["citation_edges"] == []
        kept_node = next(n for n in payload["nodes"] if n["number"] == 5)
        assert kept_node["title"] == "Alpha"


# ── Stats ──


class TestStats:
    def test_isolated_node_count(self):
        decisions = [
            make_decision(2, supersedes="1"),
            make_decision(1, status="superseded", superseded_by="2"),
            make_decision(9, title="Standalone"),
        ]
        payload = build_graph_payload(decisions)
        assert payload["stats"]["isolated_node_count"] == 1

    def test_stats_counts_consistent(self):
        payload = build_graph_payload(mixed_fan())
        stats = payload["stats"]
        assert stats["supersession_edge_count"] == len(payload["supersession_edges"])
        assert stats["citation_edge_count"] == len(payload["citation_edges"])
        assert stats["component_count"] == len(payload["components"])


# ── Payload version and empty input ──


class TestPayloadVersionAndEmpty:
    def test_payload_version_is_one(self):
        payload = build_graph_payload([make_decision(2)])
        assert payload["payload_version"] == 1
        assert GRAPH_PAYLOAD_VERSION == 1

    def test_no_findings_key(self):
        payload = build_graph_payload([make_decision(2)])
        assert "findings" not in payload

    def test_plan_schema_fields_present(self):
        # decision_count, max_decision_number, and branch_point_count are part
        # of the approved schema and must stay in the payload.
        payload = build_graph_payload(fan_in_13())
        assert "decision_count" in payload
        assert "max_decision_number" in payload
        assert "branch_point_count" in payload["stats"]

    def test_empty_input_well_formed(self):
        payload = build_graph_payload([])
        assert payload["payload_version"] == 1
        assert payload["nodes"] == []
        assert payload["supersession_edges"] == []
        assert payload["citation_edges"] == []
        assert payload["components"] == []
        assert payload["open_questions"] == []
        assert payload["decision_count"] == 0
        assert payload["max_decision_number"] == 0
        assert payload["stats"]["duplicate_numbers"] == []

    def test_scaffold_only_yields_empty_nodes(self):
        payload = build_graph_payload([make_decision(1, title="Initial project setup")])
        assert payload["nodes"] == []
        assert payload["decision_count"] == 0
