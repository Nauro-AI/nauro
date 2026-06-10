"""Pure builder for the decision-graph payload.

``build_graph_payload`` takes already-parsed ``Decision`` objects and an
optional parsed ``OpenQuestionsFile`` and returns one versioned JSON-shaped
dict: nodes, supersession edges, citation edges, connected supersession
components with explicit branch points, filtered open questions, and summary
stats. Every renderer (the CLI HTML writer today, a hosted view later)
consumes this one shape.

The builder is pure: no I/O, no clock read, no randomness. Ordering is fully
deterministic so two builds over the same input produce byte-identical JSON.

Edge semantics. Supersession edges are the union of both frontmatter
directions. The recorded store convention is a scalar ``supersedes`` carrying
one forward edge per retirement and back-only ``superseded_by`` refs for the
rest of a one-to-many retirement, with refs in the canonical plain-integer
form enforced at the model boundary. The union reconstructs every retirement
relationship as a directed edge ``(from, to)`` where ``from`` supersedes
``to``. Citation edges come from string-scanning decision bodies for
D-references (the shared ``parsing.scan_decision_references`` grammar) and are
a separate, lower-signal layer.

Plain string operations only; no regex.
"""

from __future__ import annotations

from nauro_core.decision_model import Decision
from nauro_core.parsing import first_sentence_end, scan_decision_references
from nauro_core.questions import OpenQuestionsFile
from nauro_core.validation import is_scaffold_seed

# Graph payload schema version. Bump when the payload schema changes (a new
# field, a renamed key, a changed value shape).
GRAPH_PAYLOAD_VERSION = 2


def build_graph_payload(
    decisions: list[Decision],
    questions: OpenQuestionsFile | None = None,
    project: str = "",
    include_bodies: bool = False,
) -> dict:
    """Build the decision-graph payload from parsed inputs. Pure; no I/O.

    Args:
        decisions: Parsed ``Decision`` objects. The scaffold seed (num 1 with
            the scaffold title) is excluded. When two files resolve to the same
            number one is kept (see ``_collect_nodes`` for the ordering rule)
            and the dropped number is recorded in ``stats.duplicate_numbers``.
            Only the kept decisions contribute edges and citations, so a dropped
            duplicate never fabricates an edge attributed to the kept node.
        questions: Parsed open-questions file, or None. Only unresolved entries
            appear in the payload; each body is capped to its first line or
            sentence, and each entry carries the in-range decision numbers its
            full body references.
        project: Display name for the rendered title. Carried through verbatim.
        include_bodies: When True, each node dict gains a ``"body"`` key holding
            the decision's full body markdown. Default False keeps the artifact
            titles-and-metadata-only so the rendered file carries no decision
            prose unless the caller asks for it (sensitivity posture). The key
            is omitted entirely when False rather than emitted empty.

    Returns:
        A JSON-shaped dict matching the v2 payload schema.
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
    """Return ``(kept_decisions, duplicate_numbers)`` after scaffold and dedup.

    The scaffold seed is dropped via the shared ``is_scaffold_seed`` predicate.
    Decisions are then ordered by a total deterministic key so duplicate-number
    resolution does not depend on input order: the first decision for each
    number is kept, every later file resolving to that number is recorded in
    ``duplicate_numbers``.

    The dedup key is ``(num, title, date, status, confidence, body)``. This is
    a total order over the parsed fields, so two files sharing a number (even
    with identical titles) resolve identically regardless of input order. It
    does NOT match the store layer's duplicate resolution: ``get_decision``
    resolves a number to the first matching on-disk file stem
    (``operations/decision_lookup.find_decision_stem_by_num``), and the parsed
    ``Decision`` model does not retain its source stem, so the graph cannot
    reproduce stem order. For a store with duplicate numbers the graph and
    ``get_decision`` can therefore disagree on which file is "D5". Duplicate
    numbers are a local-only anomaly the payload surfaces in
    ``stats.duplicate_numbers`` rather than hides.
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


def _node_dict(d: Decision, include_bodies: bool) -> dict:
    """Project a ``Decision`` onto the node schema.

    Parallel to ``operations/results.DecisionSummary`` (the list_decisions row
    projection); the two differ only in the type key name (``decision_type``
    here, ``type`` there) and the date being required here. Kept separate so a
    renderer change does not perturb the tool envelope.

    When ``include_bodies`` is True the full body markdown is carried under a
    ``"body"`` key; when False the key is omitted entirely (no empty string), so
    the default artifact stays titles-and-metadata-only.
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
) -> tuple[list[dict], set[tuple[int, int]]]:
    """Collect supersession edges as the deduped union of both directions.

    Iterates only the kept decisions, so a decision dropped during dedup never
    contributes an edge attributed to the node that survived under its number.

    ``supersedes: X`` on decision N yields the forward edge ``(N, X)``;
    ``superseded_by: X`` on decision N yields ``(X, N)`` (X is newer, so X
    supersedes N). Both refs are canonical plain-integer strings per the model
    boundary. An edge is kept only when both endpoints are live nodes and the
    two endpoints differ.

    Returns ``(edges, pairs)`` where ``pairs`` is the set the caller reuses to
    exclude already-superseded relationships from the citation layer.
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
) -> list[dict]:
    """Scan kept decisions' bodies for D-references and emit citation edges.

    Iterates only the kept decisions, so a dropped duplicate contributes no
    citation attributed to the surviving node. Reference parsing is delegated
    to the shared ``parsing.scan_decision_references`` grammar (forms ``D70`` /
    ``D070`` / ``decision-70``, case-insensitive, alphanumeric-left-boundary
    guarded, bounded to ``1..max_decision_number``).

    A pair is excluded when it points at a non-node, is a self-reference, or is
    already a supersession relationship. The supersession exclusion is on the
    unordered pair: ``A`` citing ``B`` is dropped whenever ``A`` supersedes
    ``B`` OR ``B`` supersedes ``A``, so a back-reference body citation never
    mirrors the supersession edge in the opposite direction.
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
    supersession_edges: list[dict],
) -> tuple[list[dict], set[int], int]:
    """Group nodes into connected components with branch points.

    Returns ``(components, incident_nodes, branch_point_count)`` so the caller
    derives the isolated-node count from the adjacency built here rather than
    re-walking the edge list.

    Connectivity is undirected over the supersession edges, so a one-to-many
    fan (one retiring decision linked to many retired ones) lands in a single
    component. Only nodes touched by at least one edge form components; fully
    isolated nodes are omitted (they carry no thread). A branch point is any
    node incident to more than one edge in the same direction (fan-in on the
    ``to`` side or fan-out on the ``from`` side). Components sort by node count
    descending then by smallest member; within a component, nodes sort
    ascending and edges sort by ``(from, to)``.
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
) -> list[dict]:
    """Return unresolved open-question entries with capped bodies and references.

    Openness is annotation-authoritative via ``OpenQuestionsFile``'s public
    ``unresolved_entries`` (``resolved_by`` unset), regardless of where the
    entry sits relative to the ``## Resolved`` divider. Each included body is
    capped to its first sentence or line for display.

    ``references`` holds the decision numbers the entry's FULL body cites (body
    plus every continuation line), not just the capped display body, scanned via
    the shared ``scan_decision_references`` grammar and bounded to live nodes.
    A reference to a number with no node (out of range, or no decision file)
    is dropped. The list is sorted ascending. The renderer derives the reverse
    direction (which decision a question points at) from this field, so the
    payload carries only the question-to-decision direction.
    """
    if questions is None:
        return []
    result: list[dict] = []
    for entry in questions.unresolved_entries:
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


def _cap_to_first_unit(body: str) -> str:
    """Cap a body to its first sentence or first line, whichever ends sooner.

    The first line ends at the first newline; the first sentence ends per the
    shared ``parsing.first_sentence_end`` grammar (terminator plus boundary,
    abbreviations skipped). The shorter boundary wins, so a multi-sentence
    single line truncates to the first sentence and a multi-line body to its
    first line.
    """
    text = body.strip()
    line_end = text.find("\n")
    if line_end == -1:
        line_end = len(text)
    cut = min(line_end, first_sentence_end(text))
    return text[:cut].rstrip()
