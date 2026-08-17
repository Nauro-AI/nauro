"""Pure builder for the decision-graph payload.

``build_graph_payload`` takes parsed ``Decision`` objects and an optional parsed
``OpenQuestionsFile`` and returns one versioned JSON-shaped dict: nodes,
supersession edges, citation edges, connected components with branch points,
filtered open questions, and summary stats. Every renderer consumes this shape.

The builder is pure: no I/O, no clock, no randomness, and plain string
operations only. Ordering is fully deterministic, so two builds over the same
input produce byte-identical JSON.

Supersession edges are the union of both frontmatter directions, reconstructing
each retirement as a directed edge ``(from, to)`` where ``from`` supersedes
``to``. Citation edges come from scanning bodies with the shared
``parsing.scan_decision_references`` grammar and are a lower-signal layer.
"""

from __future__ import annotations

from typing import TypedDict

from nauro_core.decision_model import Decision
from nauro_core.parsing import _cap_to_first_unit, scan_decision_references
from nauro_core.questions import OpenQuestionsFile
from nauro_core.validation import is_scaffold_seed

# Graph payload schema version. Bump when the payload schema changes (a new
# field, a renamed key, a changed value shape).
GRAPH_PAYLOAD_VERSION = 2


# Payload shapes. These annotate the existing dict literals only; every dict is
# a plain dict at runtime, so the emitted JSON is unchanged. Module-private (not
# in ``__all__``): the payload is consumed by name-agnostic renderers.
GraphEdge = TypedDict("GraphEdge", {"from": int, "to": int})


class GraphNodeBase(TypedDict):
    """The always-present keys of a graph node (see ``_node_dict``)."""

    number: int
    title: str
    status: str
    decision_type: str | None
    confidence: str
    date: str


class GraphNode(GraphNodeBase, total=False):
    """A graph node; ``body`` is present only when bodies are included."""

    body: str


class GraphComponent(TypedDict):
    """One connected supersession component (see ``_build_components``)."""

    nodes: list[int]
    edges: list[GraphEdge]
    branch_points: list[int]


class GraphStats(TypedDict):
    """Summary counts carried under the payload's ``stats`` key."""

    isolated_node_count: int
    supersession_edge_count: int
    citation_edge_count: int
    component_count: int
    branch_point_count: int
    duplicate_numbers: list[int]


class GraphOpenQuestion(TypedDict):
    """One genuinely-open question entry in the payload."""

    id: str
    body: str
    references: list[int]


class GraphPayload(TypedDict):
    """The full versioned decision-graph payload (see ``build_graph_payload``)."""

    payload_version: int
    project: str
    decision_count: int
    max_decision_number: int
    nodes: list[GraphNode]
    supersession_edges: list[GraphEdge]
    citation_edges: list[GraphEdge]
    components: list[GraphComponent]
    open_questions: list[GraphOpenQuestion]
    stats: GraphStats


def build_graph_payload(
    decisions: list[Decision],
    questions: OpenQuestionsFile | None = None,
    project: str = "",
    include_bodies: bool = False,
) -> GraphPayload:
    """Build the graph payload from parsed ``decisions`` and optional ``questions``;
    ``project`` is a display name carried verbatim. The scaffold seed drops out,
    duplicates collapse into ``stats.duplicate_numbers``, ``include_bodies`` adds prose.
    """
    kept, duplicate_numbers = _collect_nodes(decisions)
    nodes = [_node_dict(d, include_bodies) for d in kept]
    node_numbers = {d.num for d in kept}
    max_decision_number = max(node_numbers) if node_numbers else 0

    supersession_edges, supersession_pairs = _collect_supersession_edges(kept, node_numbers)
    citation_edges = _scan_citation_pairs(
        kept, node_numbers, max_decision_number, supersession_pairs
    )
    components, incident, branch_point_count = _build_components(supersession_edges)
    open_questions = _filter_open_questions(questions, node_numbers, max_decision_number)

    isolated_node_count = len(node_numbers - incident)

    return {
        "payload_version": GRAPH_PAYLOAD_VERSION,
        "project": project,
        "decision_count": len(nodes),
        "max_decision_number": max_decision_number,
        "nodes": nodes,
        "supersession_edges": supersession_edges,
        "citation_edges": citation_edges,
        "components": components,
        "open_questions": open_questions,
        "stats": {
            "isolated_node_count": isolated_node_count,
            "supersession_edge_count": len(supersession_edges),
            "citation_edge_count": len(citation_edges),
            "component_count": len(components),
            "branch_point_count": branch_point_count,
            "duplicate_numbers": duplicate_numbers,
        },
    }


def _collect_nodes(decisions: list[Decision]) -> tuple[list[Decision], list[int]]:
    """Return ``(kept_decisions, duplicate_numbers)``: the scaffold seed drops out and
    duplicates resolve by a total order over parsed fields, not by input order and
    not the way ``get_decision`` resolves a number to the first on-disk stem.
    """
    ordered = sorted(
        (d for d in decisions if not is_scaffold_seed(d)),
        key=lambda d: (
            d.num,
            d.title,
            d.date.isoformat(),
            d.status.value,
            d.confidence.value,
            d.body,
        ),
    )
    kept: list[Decision] = []
    seen: set[int] = set()
    duplicate_numbers: list[int] = []
    for d in ordered:
        if d.num in seen:
            if d.num not in duplicate_numbers:
                duplicate_numbers.append(d.num)
            continue
        seen.add(d.num)
        kept.append(d)
    # ``ordered`` is already ascending by ``num`` (the leading key), so ``kept``
    # is ascending by number and ``duplicate_numbers`` ascends as encountered.
    return kept, duplicate_numbers


def _node_dict(d: Decision, include_bodies: bool) -> GraphNode:
    """Project a ``Decision`` onto the node schema.

    ``include_bodies`` adds the body markdown under ``"body"``; otherwise the key is omitted.
    """
    decision_type = d.decision_type.value if d.decision_type is not None else None
    node = {
        "number": d.num,
        "title": d.title,
        "status": d.status.value,
        "decision_type": decision_type,
        "confidence": d.confidence.value,
        "date": d.date.isoformat(),
    }
    if include_bodies:
        node["body"] = d.body
    return node


def _collect_supersession_edges(
    kept: list[Decision], node_numbers: set[int]
) -> tuple[list[GraphEdge], set[tuple[int, int]]]:
    """Return ``(edges, pairs)``, the deduped union of both ref directions:
    ``supersedes: X`` on N yields ``(N, X)``, ``superseded_by: X`` yields ``(X, N)``.
    Only kept decisions contribute, and both endpoints must be distinct live nodes.
    """
    pairs: set[tuple[int, int]] = set()
    for d in kept:
        if d.supersedes is not None:
            target = int(d.supersedes)
            if target in node_numbers and target != d.num:
                pairs.add((d.num, target))
        if d.superseded_by is not None:
            newer = int(d.superseded_by)
            if newer in node_numbers and newer != d.num:
                pairs.add((newer, d.num))
    edges = [{"from": a, "to": b} for a, b in sorted(pairs)]
    return edges, pairs


def _scan_citation_pairs(
    kept: list[Decision],
    node_numbers: set[int],
    max_decision_number: int,
    supersession_pairs: set[tuple[int, int]],
) -> list[GraphEdge]:
    """Scan kept decisions' bodies for D-references and emit citation edges through the
    shared ``parsing.scan_decision_references`` grammar, bounded to
    ``1..max_decision_number``. Drops non-nodes, self-references, and supersessions.
    """
    pairs: set[tuple[int, int]] = set()
    for d in kept:
        cited = scan_decision_references(d.body, max_decision_number)
        for target in cited:
            if target == d.num or target not in node_numbers:
                continue
            if (d.num, target) in supersession_pairs or (target, d.num) in supersession_pairs:
                continue
            pairs.add((d.num, target))
    return [{"from": a, "to": b} for a, b in sorted(pairs)]


def _build_components(
    supersession_edges: list[GraphEdge],
) -> tuple[list[GraphComponent], set[int], int]:
    """Return ``(components, incident_nodes, branch_point_count)``. Connectivity is
    undirected over supersession edges, so a one-to-many fan is one component;
    isolated nodes are omitted. A branch point has two or more edges in one direction.
    """
    adjacency: dict[int, set[int]] = {}
    out_degree: dict[int, int] = {}
    in_degree: dict[int, int] = {}
    for e in supersession_edges:
        a, b = e["from"], e["to"]
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
        out_degree[a] = out_degree.get(a, 0) + 1
        in_degree[b] = in_degree.get(b, 0) + 1

    # Assign each node to a component id in one traversal, then bucket the edges
    # by component in one pass so edge collection is O(E), not O(components * E).
    component_of: dict[int, int] = {}
    members_by_component: list[set[int]] = []
    # Iterate incident nodes in ascending order so traversal order, and thus
    # the smallest-member tiebreak, is deterministic. The cycle guard is the
    # ``component_of`` map: a back-reference revisits an already-assigned node
    # and the frontier loop terminates rather than recurring forever.
    for seed in sorted(adjacency):
        if seed in component_of:
            continue
        cid = len(members_by_component)
        members: set[int] = set()
        frontier = [seed]
        while frontier:
            node = frontier.pop()
            if node in component_of:
                continue
            component_of[node] = cid
            members.add(node)
            for neighbor in adjacency[node]:
                if neighbor not in component_of:
                    frontier.append(neighbor)
        members_by_component.append(members)

    edges_by_component: list[list[tuple[int, int]]] = [[] for _ in members_by_component]
    for e in supersession_edges:
        edges_by_component[component_of[e["from"]]].append((e["from"], e["to"]))

    components: list[dict] = []
    for members, member_edges in zip(members_by_component, edges_by_component, strict=True):
        branch_points = sorted(
            node for node in members if out_degree.get(node, 0) > 1 or in_degree.get(node, 0) > 1
        )
        components.append(
            {
                "nodes": sorted(members),
                "edges": [{"from": a, "to": b} for a, b in sorted(member_edges)],
                "branch_points": branch_points,
            }
        )

    components.sort(key=lambda c: (-len(c["nodes"]), c["nodes"][0]))
    branch_point_count = sum(len(c["branch_points"]) for c in components)
    return components, set(component_of), branch_point_count


def _filter_open_questions(
    questions: OpenQuestionsFile | None,
    node_numbers: set[int],
    max_decision_number: int,
) -> list[GraphOpenQuestion]:
    """Return genuinely-open entries (``resolved_by`` unset, discovery pointers
    excluded) with each body capped to its first sentence or line. ``references``
    holds the ascending live-node decision numbers the entry's full body cites.
    """
    if questions is None:
        return []
    result: list[dict] = []
    for entry in questions.genuine_open_entries:
        full_body = "\n".join([entry.body, *entry.continuation])
        cited = scan_decision_references(full_body, max_decision_number)
        references = sorted(n for n in cited if n in node_numbers)
        result.append(
            {
                "id": entry.id,
                "body": _cap_to_first_unit(entry.body),
                "references": references,
            }
        )
    return result
