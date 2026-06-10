"""Render a decision-graph payload into one self-contained HTML document.

The output is a single read-only file: inline CSS, inline vanilla JS, inline
SVG, no external network requests, no third-party assets. The payload is
embedded once as a ``<script type="application/json">`` block; the page reads it
at load time to drive cross-view search and filters and to populate the shared
detail panel.

The page is a four-view single-file application over the same payload:

* Graph (default): a node-link map of the whole store on a deterministic
  layout (no force-directed physics). Supersession threads are star clusters,
  isolated decisions sit in per-category sunflower discs, and the clusters are
  packed with a golden-angle spiral. Node radius is degree, hue is category,
  fill is status, opacity is confidence. Pan and zoom are vanilla JS over the
  SVG viewBox.
* Lineage: one drawn DAG per supersession component, SVG edges, time flowing
  left to right. Generation columns are the longest distance from the
  component's roots along supersession edges; consolidation fan-ins are the most
  prominent objects.
* Timeline: a true date axis (earliest to latest decision date) with category
  lanes; marks are positioned by real date, not index, and same-day same-lane
  marks stack so a busy day reads as a visible column.
* Browse: active decisions grouped by category, each expanding in place to a
  detail panel of relations and linked questions.

Open questions are integrated: a question's decision references render as links
into the detail panel, and each referenced decision badges back to its
questions. References come from the payload's ``references`` field; there is no
client-side reference parsing. The renderer and the payload ship in the same
artifact, so there is no legacy-payload fallback path: a v2 payload is assumed.

This module deliberately consumes only the payload dict, never a store or any
I/O, so relocating it (for example to a hosted renderer that builds the same
payload) is mechanical.

Built with f-strings and string templates only; no Jinja2, no regex. All
authored copy is neutral and diagnostic, uses "decision" as the framing noun,
carries no em-dashes, and keeps "memory" and "context" out of headings.
"""

from __future__ import annotations

import json
import math
from datetime import date as _date
from html import escape as _html_escape

from nauro_core.decision_model import DECISION_TYPE_VALUES

# Category order for grouping and lane assignment, derived from the canonical
# decision-type values so a newly added type gets its own group automatically
# rather than falling silently into "other". Decisions with a null or
# unrecognized type land in the trailing "other" group so none is dropped.
_CATEGORY_ORDER = list(DECISION_TYPE_VALUES)
_OTHER_CATEGORY = "other"

# Category keys that read better with a custom label than the generic
# underscore-to-space transform.
_CATEGORY_LABEL_OVERRIDES = {"api_design": "API design"}

# Muted per-category hues for the Graph view node fills. Kept consistent with
# the warm paper palette: each is a desaturated tone distinct enough to read as
# a category at a glance without turning the canvas into a rainbow. The "other"
# bucket (null or unknown type) uses a neutral grey.
_CATEGORY_HUE = {
    "architecture": "#5a8fb0",
    "api_design": "#c08a3e",
    "infrastructure": "#6fa67a",
    "pattern": "#a071b0",
    "refactor": "#c87f6a",
    "data_model": "#5fa8a0",
    "other": "#8a8678",
}

# Lineage layout geometry, in SVG user units. ``_ROW_PITCH`` is the vertical
# distance between adjacent row slots; it is the minimum gap the collision
# resolver enforces, so it must stay larger than ``_NODE_HEIGHT`` to keep nodes
# from overlapping.
_COL_WIDTH = 220
_ROW_PITCH = 78
_NODE_WIDTH = 168
_NODE_HEIGHT = 56
_MARGIN_X = 24
_MARGIN_Y = 24

# Timeline geometry.
_TL_LANE_HEIGHT = 44
_TL_LEFT_GUTTER = 132
_TL_RIGHT_PAD = 40
_TL_TOP_PAD = 36
_TL_PLOT_WIDTH = 1040
# Vertical offset between marks that share a (date, lane) cell, so a busy day
# reads as a visible stack instead of a single hidden overlap.
_TL_STACK_STEP = 7

# Graph-canvas layout geometry, in SVG user units. The golden angle (in
# radians) drives both the within-cluster ring spread and the canvas packing
# spiral; using one deterministic constant keeps the whole layout reproducible.
_GOLDEN_ANGLE = 2.399963229728653
_GRAPH_BASE_RADIUS = 10.0  # node radius at the floor degree
_GRAPH_MAX_RADIUS = 30.0  # node radius cap
_GRAPH_RING_STEP = 96.0  # radial distance between supersession-distance rings
_GRAPH_DISC_SPACING = 34.0  # sunflower spacing constant (dot pitch)
_GRAPH_LABEL_CLEARANCE = 26.0  # extra cluster radius reserved for labels
_GRAPH_CLUSTER_PADDING = 40.0  # gap enforced between packed clusters
_GRAPH_SPIRAL_STEP = 26.0  # radial step walked along the packing spiral
_GRAPH_HUB_LABEL_LIMIT = 18  # hubs labelled at the default zoom


def render_html(payload: dict, *, generated_at: str) -> str:
    """Render the graph payload to a complete HTML document.

    Args:
        payload: The dict returned by ``build_graph_payload`` (v2 shape).
            Embedded verbatim and also read on the Python side to build the
            static markup for all four views.
        generated_at: A human-readable generation timestamp for the footer. The
            payload itself carries no timestamp (the builder is pure), so the
            caller stamps the time here.

    Returns:
        One self-contained HTML document as a string.
    """
    project = payload.get("project") or "this project"
    nodes = payload.get("nodes", [])

    embedded = _embed_payload(payload)
    title_text = _esc(f"Nauro decision graph: {project}")
    footer = _render_footer(payload, generated_at)

    if not nodes:
        # A store can have zero decisions yet still carry flagged open
        # questions, so the empty branch still renders the questions list.
        body = (
            _render_header_strip(payload)
            + _render_empty_state()
            + _render_questions_section(payload)
        )
    else:
        relations = _supersession_relations(payload)
        question_refs = _question_reference_map(payload)
        body = (
            _render_header_strip(payload)
            + _render_graph_view(payload, relations, question_refs)
            + _render_lineage_view(payload, relations)
            + _render_timeline_view(payload)
            + _render_browse_view(payload, relations, question_refs)
            + _render_questions_section(payload)
            + _render_detail_store(payload, relations, question_refs)
        )

    return _DOCUMENT_TEMPLATE.format(
        title=title_text,
        styles=_STYLES,
        body=body,
        footer=footer,
        payload_json=embedded,
        script=_SCRIPT,
    )


# ── Payload embedding and escaping ──


def _embed_payload(payload: dict) -> str:
    """Serialize the payload for a ``<script type="application/json">`` block.

    The ``.replace`` chain below is the only thing that prevents a string value
    (a title or body containing ``</script>``, say) from terminating the script
    element early: it rewrites ``<``, ``>`` and ``&`` to their JSON unicode
    escapes, which are valid only inside JSON string literals, which is the only
    place those characters occur in this document. Do not remove it on the
    assumption that ``ensure_ascii`` covers the breakout; it does not.
    ``ensure_ascii`` only forces non-ASCII to ``\\uXXXX`` and leaves ``<`` and
    ``>`` intact.
    """
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _esc(text: str) -> str:
    """Escape text for HTML markup or a double-quoted attribute value.

    ``html.escape`` with ``quote=True`` escapes both quote styles, so this is
    safe in attribute values regardless of the surrounding quote character.
    Every payload-derived string rendered into markup or an attribute passes
    through here.
    """
    return _html_escape(text, quote=True)


# ── Category helpers ──


def _category_of(decision_type) -> str:
    """Map a node's decision_type onto a category key."""
    if decision_type in _CATEGORY_ORDER:
        return decision_type
    return _OTHER_CATEGORY


def _category_label(category: str) -> str:
    """Human label for a category key."""
    return _CATEGORY_LABEL_OVERRIDES.get(category, category.replace("_", " "))


# ── Relation and reference maps ──


def _supersession_relations(payload: dict) -> dict[int, dict[str, list[int]]]:
    """Map each node to the decisions it supersedes and is superseded by.

    An edge ``(from, to)`` means ``from`` supersedes ``to``. For each node this
    returns ``{"supersedes": [...], "superseded_by": [...]}`` with both lists
    ascending. Edges come straight from the payload, so the relations shown on
    the detail panels reflect exactly the edges the components encode.
    """
    relations: dict[int, dict[str, list[int]]] = {}
    for edge in payload.get("supersession_edges", []):
        a, b = edge["from"], edge["to"]
        relations.setdefault(a, {"supersedes": [], "superseded_by": []})
        relations.setdefault(b, {"supersedes": [], "superseded_by": []})
        relations[a]["supersedes"].append(b)
        relations[b]["superseded_by"].append(a)
    for rel in relations.values():
        rel["supersedes"].sort()
        rel["superseded_by"].sort()
    return relations


def _citation_map(payload: dict) -> dict[int, list[int]]:
    """Map each node to the decisions it cites in its body (the "cited by" view).

    A citation edge ``(from, to)`` means ``from`` cites ``to``. The detail panel
    shows, for a node, which decisions cite it (the reverse of the edge), which
    is what "cited by" means to a reader. Lists ascend.
    """
    cited_by: dict[int, list[int]] = {}
    for edge in payload.get("citation_edges", []):
        cited_by.setdefault(edge["to"], []).append(edge["from"])
    for refs in cited_by.values():
        refs.sort()
    return cited_by


def _question_reference_map(payload: dict) -> dict[int, list[str]]:
    """Map each decision number to the question ids that reference it.

    The payload carries the question-to-decision direction (each question's
    ``references``); this inverts it so a decision's detail panel can badge the
    questions pointing at it. Question ids keep first-seen (payload) order, which
    is questions-first then resolved per the builder's filter.
    """
    by_decision: dict[int, list[str]] = {}
    for q in payload.get("open_questions", []):
        qid = q.get("id", "")
        for num in q.get("references", []):
            bucket = by_decision.setdefault(num, [])
            if qid not in bucket:
                bucket.append(qid)
    return by_decision


# ── Graph-canvas deterministic layout ──
#
# The Graph view is a node-link map laid out with zero randomness. Every step
# below is a pure function of the payload, so two renders of the same store are
# byte-identical. There is no force-directed physics: positions come from
# closed-form geometry (rings, phyllotaxis discs, a golden-angle packing
# spiral), not from iterative relaxation toward an energy minimum.


def _node_degree(payload: dict) -> dict[int, int]:
    """Total degree per node = supersession plus citation endpoints.

    Degree drives node radius, so both edge layers count: a consolidation hub is
    large because many supersession edges touch it, and a heavily cited decision
    grows even with no supersession edge.
    """
    degree: dict[int, int] = {n["number"]: 0 for n in payload.get("nodes", [])}
    for edge in payload.get("supersession_edges", []):
        if edge["from"] in degree:
            degree[edge["from"]] += 1
        if edge["to"] in degree:
            degree[edge["to"]] += 1
    for edge in payload.get("citation_edges", []):
        if edge["from"] in degree:
            degree[edge["from"]] += 1
        if edge["to"] in degree:
            degree[edge["to"]] += 1
    return degree


def _node_radius(degree: int, max_degree: int) -> float:
    """Map a node's degree onto a floored, capped radius.

    Radius scales with the square root of degree (so area, not radius, tracks
    degree) between the floor and the cap. With no edges anywhere every node
    sits at the floor.
    """
    if max_degree <= 0:
        return _GRAPH_BASE_RADIUS
    span = _GRAPH_MAX_RADIUS - _GRAPH_BASE_RADIUS
    return _GRAPH_BASE_RADIUS + span * math.sqrt(degree / max_degree)


def _component_center(component: dict, node_by_number: dict[int, dict]) -> int:
    """Pick the cluster center: the terminal active retirer of the thread.

    A terminal node is one nothing else in the component supersedes (no incoming
    supersession edge, i.e. it is never a ``to``). Among terminals, prefer active
    ones, then take the highest number so multi-terminal threads resolve
    deterministically. Falls back to the highest number overall if the edge set
    somehow leaves no terminal (a pure cycle).
    """
    members = set(component["nodes"])
    superseded_targets = {e["to"] for e in component["edges"]}
    terminals = [n for n in members if n not in superseded_targets]
    if not terminals:
        terminals = sorted(members)
    active_terminals = [n for n in terminals if node_by_number.get(n, {}).get("status") == "active"]
    pool = active_terminals or terminals
    return max(pool)


def _supersession_distance(component: dict, center: int) -> dict[int, int]:
    """Distance in supersession edges from each node to the cluster center.

    Undirected breadth-first over the component's edges, so a node's ring is how
    many retirement hops separate it from the terminal center. The center sits at
    distance 0 (the innermost point); a pure fan-in puts every child at distance
    1 (one ring), and a chain steps outward one ring per generation.
    """
    adjacency: dict[int, list[int]] = {n: [] for n in component["nodes"]}
    for e in component["edges"]:
        adjacency[e["from"]].append(e["to"])
        adjacency[e["to"]].append(e["from"])
    distance: dict[int, int] = {center: 0}
    frontier = [center]
    while frontier:
        nxt: list[int] = []
        for node in frontier:
            for neighbor in sorted(adjacency[node]):
                if neighbor not in distance:
                    distance[neighbor] = distance[node] + 1
                    nxt.append(neighbor)
        frontier = sorted(nxt)
    # Any node unreached by the walk (disconnected within the component, which
    # should not happen for a connected component) lands on the outermost ring.
    fallback = max(distance.values(), default=0) + 1
    for n in component["nodes"]:
        distance.setdefault(n, fallback)
    return distance


def _layout_component(
    component: dict, node_by_number: dict[int, dict]
) -> tuple[dict[int, tuple[float, float]], float, int]:
    """Lay out one supersession component as concentric rings around its center.

    Returns ``(local_positions, bounding_radius, center_number)`` with positions
    relative to the cluster center at (0, 0). Nodes on a ring are sorted by
    number and spread evenly around the circle; alternate rings are rotated half
    a step so spokes on adjacent rings do not align. A pure fan-in becomes a
    star, a chain a short radial run, and a merge two arms joining the center.
    """

    center = _component_center(component, node_by_number)
    distance = _supersession_distance(component, center)

    by_ring: dict[int, list[int]] = {}
    for num in component["nodes"]:
        by_ring.setdefault(distance[num], []).append(num)
    for ring in by_ring.values():
        ring.sort()

    positions: dict[int, tuple[float, float]] = {center: (0.0, 0.0)}
    max_ring = max(by_ring) if by_ring else 0
    for ring_index in sorted(by_ring):
        if ring_index == 0:
            continue
        members = by_ring[ring_index]
        radius = ring_index * _GRAPH_RING_STEP
        count = len(members)
        # Half-step rotation on alternate rings so adjacent-ring spokes stagger.
        phase = (_GOLDEN_ANGLE if ring_index % 2 else 0.0) + math.pi / max(count, 1)
        for slot, num in enumerate(members):
            angle = phase + 2 * math.pi * slot / count
            positions[num] = (radius * math.cos(angle), radius * math.sin(angle))

    bounding = max_ring * _GRAPH_RING_STEP + _GRAPH_MAX_RADIUS + _GRAPH_LABEL_CLEARANCE
    return positions, bounding, center


def _layout_disc(
    members: list[int],
) -> tuple[dict[int, tuple[float, float]], float]:
    """Lay out a category of isolated decisions as a phyllotaxis (sunflower) disc.

    Node k sits at angle ``k * golden_angle`` and radius ``c * sqrt(k)`` (the
    sunflower spiral), nodes ordered by number. The square-root radius keeps the
    dot density uniform across the disc, so it reads as a filled circle rather
    than a spiral arm. Returns positions relative to the disc center at (0, 0)
    and the disc bounding radius.
    """

    ordered = sorted(members)
    positions: dict[int, tuple[float, float]] = {}
    max_r = 0.0
    for k, num in enumerate(ordered):
        radius = _GRAPH_DISC_SPACING * math.sqrt(k)
        angle = k * _GOLDEN_ANGLE
        positions[num] = (radius * math.cos(angle), radius * math.sin(angle))
        max_r = max(max_r, radius)
    bounding = max_r + _GRAPH_MAX_RADIUS + _GRAPH_LABEL_CLEARANCE
    return positions, bounding


def _pack_clusters(clusters: list[dict]) -> None:
    """Place each cluster's center on the canvas with a golden-angle spiral.

    Each cluster is a circle of known ``radius``. Sorted by radius descending,
    the largest lands at the canvas origin; each subsequent cluster walks a
    golden-angle spiral outward from the origin and takes the first position
    where its circle clears every already-placed circle (plus padding). The walk
    is deterministic, so the packing is reproducible and looks organic without
    any physics. Mutates each cluster dict in place with a ``center`` key.
    """

    ordered = sorted(clusters, key=lambda c: (-c["radius"], c["sort_key"]))
    placed: list[tuple[float, float, float]] = []
    for cluster in ordered:
        radius = cluster["radius"]
        if not placed:
            cluster["center"] = (0.0, 0.0)
            placed.append((0.0, 0.0, radius))
            continue
        step = 0
        while True:
            step += 1
            spiral_r = _GRAPH_SPIRAL_STEP * math.sqrt(step)
            angle = step * _GOLDEN_ANGLE
            cx = spiral_r * math.cos(angle)
            cy = spiral_r * math.sin(angle)
            clear = True
            for px, py, pr in placed:
                min_gap = radius + pr + _GRAPH_CLUSTER_PADDING
                if (cx - px) ** 2 + (cy - py) ** 2 < min_gap * min_gap:
                    clear = False
                    break
            if clear:
                cluster["center"] = (cx, cy)
                placed.append((cx, cy, radius))
                break


def build_graph_layout(payload: dict) -> dict:
    """Compute the full deterministic node-link layout for the Graph view.

    Returns a dict with: ``positions`` (number -> absolute (x, y)), ``radii``
    (number -> node radius), ``clusters`` (metadata for cluster labels), and
    ``bounds`` (min_x, min_y, max_x, max_y of all node centers). Pure and
    randomness-free, so rendering the same payload twice is byte-identical.
    """
    nodes = payload.get("nodes", [])
    node_by_number = {n["number"]: n for n in nodes}
    degree = _node_degree(payload)
    max_degree = max(degree.values(), default=0)
    radii = {num: _node_radius(deg, max_degree) for num, deg in degree.items()}

    clusters: list[dict] = []

    # Component clusters (supersession threads).
    for component in payload.get("components", []):
        local, bounding, center = _layout_component(component, node_by_number)
        clusters.append(
            {
                "kind": "component",
                "local": local,
                "radius": bounding,
                "center_number": center,
                # Pack larger threads toward the middle; the tiebreak string is
                # kept homogeneous with the disc clusters so a radius tie between
                # a component and a disc never compares an int against a str.
                "sort_key": (-len(component["nodes"]), f"c{center:06d}"),
                "label": "",
            }
        )

    # Isolated decisions grouped into per-category sunflower discs.
    incident = set()
    for component in payload.get("components", []):
        incident.update(component["nodes"])
    isolated_by_category: dict[str, list[int]] = {}
    for node in nodes:
        if node["number"] in incident:
            continue
        cat = _category_of(node.get("decision_type"))
        isolated_by_category.setdefault(cat, []).append(node["number"])

    for category in _present_categories([n for n in nodes if n["number"] not in incident]):
        members = isolated_by_category.get(category, [])
        if not members:
            continue
        local, bounding = _layout_disc(members)
        clusters.append(
            {
                "kind": "disc",
                "local": local,
                "radius": bounding,
                "category": category,
                "sort_key": (-len(members), f"d{category}"),
                "label": _category_label(category),
            }
        )

    _pack_clusters(clusters)

    positions: dict[int, tuple[float, float]] = {}
    for cluster in clusters:
        cx, cy = cluster["center"]
        for num, (lx, ly) in cluster["local"].items():
            positions[num] = (cx + lx, cy + ly)

    if positions:
        xs = [p[0] for p in positions.values()]
        ys = [p[1] for p in positions.values()]
        bounds = (min(xs), min(ys), max(xs), max(ys))
    else:
        bounds = (0.0, 0.0, 0.0, 0.0)

    return {
        "positions": positions,
        "radii": radii,
        "degree": degree,
        "clusters": clusters,
        "bounds": bounds,
    }


# ── Header strip ──


def _render_header_strip(payload: dict) -> str:
    """Render the always-visible header strip: counts, search box, filters.

    Confidence stands in for the founder spec's "priority" filter because the
    Decision model has no priority field; it is rendered as the secondary
    metadata and drives that filter. The view tabs switch the four views.
    """
    nodes = payload.get("nodes", [])
    project = payload.get("project") or "this project"
    active = sum(1 for n in nodes if n.get("status") == "active")
    superseded = sum(1 for n in nodes if n.get("status") == "superseded")
    question_count = len(payload.get("open_questions", []))
    dates = sorted(n.get("date", "") for n in nodes if n.get("date"))
    last_date = dates[-1] if dates else "none"

    category_options = "".join(
        f'<option value="{_esc(c)}">{_esc(_category_label(c))}</option>'
        for c in _present_categories(nodes)
    )

    return (
        '<header class="page-header">'
        f"<h1>Decision graph: {_esc(project)}</h1>"
        '<div class="stat-row">'
        f'<span class="stat"><strong>{active}</strong> active</span>'
        f'<span class="stat"><strong>{superseded}</strong> superseded</span>'
        f'<span class="stat"><strong>{question_count}</strong> open questions</span>'
        f'<span class="stat">latest decision <strong>{_esc(last_date)}</strong></span>'
        "</div>"
        '<nav class="view-tabs" role="tablist">'
        '<button class="view-tab is-active" data-view="graph">Graph</button>'
        '<button class="view-tab" data-view="lineage">Lineage</button>'
        '<button class="view-tab" data-view="timeline">Timeline</button>'
        '<button class="view-tab" data-view="browse">Browse</button>'
        "</nav>"
        '<div class="controls">'
        '<label class="search">Search'
        '<input id="search-box" type="text" autocomplete="off" '
        'placeholder="D-number or title" /></label>'
        '<label class="filter">Status'
        '<select id="filter-status">'
        '<option value="all">All</option>'
        '<option value="active">Active</option>'
        '<option value="superseded">Superseded</option>'
        "</select></label>"
        '<label class="filter">Category'
        '<select id="filter-category"><option value="all">All</option>'
        f"{category_options}</select></label>"
        '<label class="filter">Confidence'
        '<select id="filter-confidence">'
        '<option value="all">All</option>'
        '<option value="high">High</option>'
        '<option value="medium">Medium</option>'
        '<option value="low">Low</option>'
        "</select></label>"
        "</div>"
        "</header>"
    )


def _present_categories(nodes: list[dict]) -> list[str]:
    """Return category keys present in the node set, in canonical order."""
    present = {_category_of(n.get("decision_type")) for n in nodes}
    keys = [c for c in _CATEGORY_ORDER if c in present]
    if _OTHER_CATEGORY in present:
        keys.append(_OTHER_CATEGORY)
    return keys


# ── View: Graph (the default node-link canvas) ──


def _confidence_opacity(confidence: str) -> float:
    """Opacity tier for a node by confidence: high 1.0, medium 0.8, low 0.6."""
    return {"high": 1.0, "medium": 0.8, "low": 0.6}.get(confidence, 0.8)


def _render_story_strip(
    payload: dict,
    relations: dict[int, dict[str, list[int]]],
    question_refs: dict[int, list[str]],
) -> str:
    """Render a compact docent strip of jump-capable buttons above the canvas.

    Each button is derived deterministically from the payload, renderer-side
    only (no client-side reference parsing). A metric that is undefined for the
    store (no edges, no questions) drops its button rather than render an empty
    one, so a sparse store shows a shorter strip or none at all.

    The four metrics, each with a ``data-story`` action the script wires up:

    * Largest consolidation: the supersession target with the most incoming
      edges. Action ``center`` (center and flash the node).
    * Open-question hotspot: the decision appearing in the most question
      reference arrays (ties: lower number). Action ``detail`` (center, flash,
      and open the detail panel listing its linked questions).
    * Recent activity: the date with the most decisions (ties: latest date).
      Action ``date`` (highlight every node on that date, staying in Graph).
    * Anchor: the most-cited active decision. Neutral wording only, no semantic
      claim. Action ``center``.
    """
    nodes = payload.get("nodes", [])
    node_by_number = {n["number"]: n for n in nodes}
    buttons: list[str] = []

    # Largest consolidation: most incoming supersession edges (a node's
    # "supersedes" list is its incoming retirements). Ties: lower number.
    best_consolidation = None
    best_count = 0
    for num in sorted(node_by_number):
        count = len(relations.get(num, {}).get("supersedes", []))
        if count > best_count:
            best_count = count
            best_consolidation = num
    if best_consolidation is not None and best_count >= 2:
        plural = "decision" if best_count == 1 else "decisions"
        buttons.append(
            _story_button(
                "center",
                best_consolidation,
                "Largest consolidation",
                f"D{best_consolidation} retires {best_count} {plural}",
            )
        )

    # Open-question hotspot: decision in the most question reference arrays.
    # Ties: lower number (sorted ascending, strict > keeps the first seen).
    hotspot = None
    hotspot_count = 0
    for num in sorted(question_refs):
        count = len(question_refs[num])
        if count > hotspot_count:
            hotspot_count = count
            hotspot = num
    if hotspot is not None and hotspot_count >= 1:
        noun = "linked open question" if hotspot_count == 1 else "linked open questions"
        buttons.append(
            _story_button(
                "detail",
                hotspot,
                "Open-question hotspot",
                f"D{hotspot} has {hotspot_count} {noun}",
            )
        )

    # Recent activity: the date carrying the most decisions. Ties: the latest
    # date wins (iterate ascending; >= lets the latest tied date overwrite).
    date_counts: dict[str, int] = {}
    for n in nodes:
        d = n.get("date", "")
        if d:
            date_counts[d] = date_counts.get(d, 0) + 1
    if date_counts:
        busiest_date = ""
        busiest_count = 0
        for d in sorted(date_counts):
            if date_counts[d] >= busiest_count:
                busiest_count = date_counts[d]
                busiest_date = d
        if busiest_count >= 2:
            plural = "decision" if busiest_count == 1 else "decisions"
            buttons.append(_story_button_date(busiest_date, busiest_count, plural))

    # Anchor: the most-cited active decision (incoming citation edges). Neutral
    # wording only. Ties: lower number.
    cited_count: dict[int, int] = {}
    for edge in payload.get("citation_edges", []):
        cited_count[edge["to"]] = cited_count.get(edge["to"], 0) + 1
    anchor = None
    anchor_count = 0
    for num in sorted(cited_count):
        if node_by_number.get(num, {}).get("status") != "active":
            continue
        if cited_count[num] > anchor_count:
            anchor_count = cited_count[num]
            anchor = num
    if anchor is not None and anchor_count >= 1:
        plural = "time" if anchor_count == 1 else "times"
        buttons.append(
            _story_button("center", anchor, "Anchor", f"D{anchor} cited {anchor_count} {plural}")
        )

    if not buttons:
        return ""
    return f'<div class="story-strip">{"".join(buttons)}</div>'


def _story_button(action: str, number: int, kicker: str, body: str) -> str:
    """Render one story-strip button targeting a decision number."""
    return (
        f'<button class="story-card" data-story="{_esc(action)}" '
        f'data-story-target="{number}">'
        f'<span class="story-kicker">{_esc(kicker)}</span>'
        f'<span class="story-body">{_esc(body)}</span></button>'
    )


def _story_button_date(date: str, count: int, plural: str) -> str:
    """Render the recent-activity story button targeting a date."""
    return (
        '<button class="story-card" data-story="date" '
        f'data-story-date="{_esc(date)}">'
        '<span class="story-kicker">Recent activity</span>'
        f'<span class="story-body">{_esc(date)}: {count} {_esc(plural)}</span></button>'
    )


def _render_graph_view(
    payload: dict,
    relations: dict[int, dict[str, list[int]]],
    question_refs: dict[int, list[str]],
) -> str:
    """Render the default Graph view: a deterministic node-link canvas.

    Decisions are nodes (radius by degree, hue by category, filled when active /
    hollow ring when superseded, opacity by confidence). Supersession edges are
    always drawn and stronger; consolidation fan-ins are heaviest. Citation
    edges are a faint always-on web. Only hubs are labelled at the default zoom;
    every node carries its label in a data attribute for hover and search. The
    SVG viewBox is fit to content so the initial view frames the whole map.
    """
    nodes = payload.get("nodes", [])
    layout = build_graph_layout(payload)
    positions = layout["positions"]
    radii = layout["radii"]
    degree = layout["degree"]
    node_by_number = {n["number"]: n for n in nodes}

    min_x, min_y, max_x, max_y = layout["bounds"]
    pad = _GRAPH_MAX_RADIUS + _GRAPH_LABEL_CLEARANCE + 20
    vb_x = min_x - pad
    vb_y = min_y - pad
    vb_w = (max_x - min_x) + 2 * pad
    vb_h = (max_y - min_y) + 2 * pad
    if vb_w <= 0:
        vb_w = 2 * pad
    if vb_h <= 0:
        vb_h = 2 * pad

    citation_layer = _graph_citation_edges(payload, positions)
    supersession_layer = _graph_supersession_edges(payload, positions, relations, node_by_number)
    node_layer, label_layer = _graph_nodes_and_labels(
        payload,
        positions,
        radii,
        degree,
        relations,
        question_refs,
        node_by_number,
        layout["clusters"],
    )

    story_strip = _render_story_strip(payload, relations, question_refs)

    viewbox = f"{vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}"
    return (
        '<main class="view view-graph is-active" data-view="graph">'
        f"{story_strip}"
        '<div class="graph-canvas">'
        f'<svg id="graph-svg" class="graph-svg" viewBox="{viewbox}" '
        f'data-fit="{viewbox}" preserveAspectRatio="xMidYMid meet" '
        'role="img" aria-label="Decision graph node-link map">'
        '<g id="graph-pan">'
        f'<g class="citation-layer">{citation_layer}</g>'
        f'<g class="supersession-layer">{supersession_layer}</g>'
        f'<g class="node-layer">{node_layer}</g>'
        f'<g class="label-layer">{label_layer}</g>'
        "</g></svg></div>"
        '<p class="graph-note">Decisions are nodes; size is connection count, '
        "hue is category. Filled nodes are active, hollow rings superseded. Bold "
        "lines are supersession; the faint web is body citations. Scroll to zoom, "
        "drag to pan, select a node for detail.</p>"
        "</main>"
    )


def _graph_citation_edges(payload: dict, positions: dict[int, tuple[float, float]]) -> str:
    """Render the faint always-on citation web as straight SVG lines.

    Each citation edge connects two node centers. The whole layer is faint by
    default (CSS opacity in the 0.06-0.12 range) and brightens for edges
    incident to the hovered or selected node via the ``data-from``/``data-to``
    hooks the script keys on.
    """
    lines: list[str] = []
    for edge in payload.get("citation_edges", []):
        a, b = edge["from"], edge["to"]
        if a not in positions or b not in positions:
            continue
        x1, y1 = positions[a]
        x2, y2 = positions[b]
        lines.append(
            f'<line class="cite-edge" x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}" data-from="{a}" data-to="{b}" />'
        )
    return "".join(lines)


def _graph_supersession_edges(
    payload: dict,
    positions: dict[int, tuple[float, float]],
    relations: dict[int, dict[str, list[int]]],
    node_by_number: dict[int, dict],
) -> str:
    """Render supersession edges as strong SVG lines, heaviest into a fan-in.

    An edge ``(from, to)`` means ``from`` supersedes ``to``; the line runs
    between the two node centers. Edges whose retirer is a consolidation target
    (active fan-in of three or more) carry the emphasis class so the converging
    bundle reads as the most prominent drawn structure on the canvas.
    """
    consolidation = {
        num
        for num in positions
        if len(relations.get(num, {}).get("supersedes", [])) >= 3
        and node_by_number.get(num, {}).get("status") == "active"
    }
    lines: list[str] = []
    for edge in payload.get("supersession_edges", []):
        a, b = edge["from"], edge["to"]
        if a not in positions or b not in positions:
            continue
        x1, y1 = positions[a]
        x2, y2 = positions[b]
        cls = "sup-edge consolidation-edge" if a in consolidation else "sup-edge"
        lines.append(
            f'<line class="{cls}" x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}" data-from="{a}" data-to="{b}" />'
        )
    return "".join(lines)


def _graph_hub_numbers(
    payload: dict,
    degree: dict[int, int],
    relations: dict[int, dict[str, list[int]]],
    node_by_number: dict[int, dict],
) -> set[int]:
    """Pick the nodes labelled at the default zoom: hubs and top-degree nodes.

    Every consolidation target (active fan-in of three or more) is a hub, plus
    the highest-degree nodes up to the label budget. Returns roughly 12-20
    numbers, deterministically chosen by degree then number.
    """
    hubs: set[int] = {
        num
        for num in degree
        if len(relations.get(num, {}).get("supersedes", [])) >= 3
        and node_by_number.get(num, {}).get("status") == "active"
    }
    by_degree = sorted(degree, key=lambda n: (-degree[n], n))
    for num in by_degree:
        if len(hubs) >= _GRAPH_HUB_LABEL_LIMIT:
            break
        hubs.add(num)
    return hubs


def _graph_nodes_and_labels(
    payload: dict,
    positions: dict[int, tuple[float, float]],
    radii: dict[int, float],
    degree: dict[int, int],
    relations: dict[int, dict[str, list[int]]],
    question_refs: dict[int, list[str]],
    node_by_number: dict[int, dict],
    clusters: list[dict],
) -> tuple[str, str]:
    """Render node circles and the hub label set.

    Each node is a circle whose fill is the category hue, drawn filled for
    active and as a hollow ring for superseded, at the confidence opacity tier.
    Nodes carry the data attributes the script needs for search, filter, hover
    label, and detail open. Hub labels render in a separate layer so they sit
    above every node.
    """
    hubs = _graph_hub_numbers(payload, degree, relations, node_by_number)
    node_svg: list[str] = []
    label_svg: list[str] = []
    for node in payload.get("nodes", []):
        num = node["number"]
        if num not in positions:
            continue
        x, y = positions[num]
        r = radii[num]
        category = _category_of(node.get("decision_type"))
        hue = _CATEGORY_HUE.get(category, _CATEGORY_HUE["other"])
        status = node.get("status", "active")
        confidence = node.get("confidence", "medium")
        opacity = _confidence_opacity(confidence)
        title = node.get("title", "")
        has_q = "1" if question_refs.get(num) else "0"
        classes = ["gnode", f"status-{_esc(status)}"]
        if num in hubs:
            classes.append("is-hub")
        node_svg.append(
            f'<circle class="{" ".join(classes)}" cx="{x:.1f}" cy="{y:.1f}" '
            f'r="{r:.1f}" fill="{hue}" fill-opacity="{opacity:.2f}" '
            f'data-number="{num}" data-title="{_esc(title)}" '
            f'data-status="{_esc(status)}" data-category="{_esc(category)}" '
            f'data-confidence="{_esc(confidence)}" data-has-questions="{has_q}" '
            f'data-date="{_esc(node.get("date", ""))}" '
            f'data-detail-trigger="{num}" tabindex="0" role="button">'
            f"<title>D{num} {_esc(title)}</title></circle>"
        )
        if num in hubs:
            label = title if len(title) <= 32 else title[:31].rstrip() + "…"
            ly = y - r - 6
            label_svg.append(
                f'<text class="gnode-label" x="{x:.1f}" y="{ly:.1f}" '
                f'text-anchor="middle" data-label-for="{num}">'
                f"D{num} {_esc(label)}</text>"
            )

    # Cluster labels for the sunflower discs sit under each disc.
    for cluster in clusters:
        if cluster["kind"] != "disc" or not cluster["label"]:
            continue
        cx, cy = cluster["center"]
        ly = cy + cluster["radius"] - _GRAPH_LABEL_CLEARANCE + 16
        label_svg.append(
            f'<text class="disc-label" x="{cx:.1f}" y="{ly:.1f}" '
            f'text-anchor="middle">{_esc(cluster["label"])}</text>'
        )

    return "".join(node_svg), "".join(label_svg)


# ── View: Browse (the card browser, demoted from default) ──


def _render_browse_view(
    payload: dict,
    relations: dict[int, dict[str, list[int]]],
    question_refs: dict[int, list[str]],
) -> str:
    """Render the Browse view: every decision grouped by category.

    All decisions render (active and superseded), so the status filter has real
    cards to act on in every state. Superseded cards are visibly distinct
    (dimmed, hollow status mark, status text). Per-group counts are truthful
    ("N active" or "N active · M superseded"). Titles wrap in full. Each card
    expands in place to its detail panel. Demoted from the default to a tab
    behind the Graph view.
    """
    nodes = payload.get("nodes", [])
    by_category: dict[str, list[dict]] = {}
    for node in nodes:
        by_category.setdefault(_category_of(node.get("decision_type")), []).append(node)

    groups: list[str] = []
    for category in _present_categories(nodes):
        cat_nodes = sorted(
            by_category[category],
            key=lambda n: (n.get("status", "active"), n.get("date", ""), n.get("number", 0)),
        )
        active = sum(1 for n in cat_nodes if n.get("status") == "active")
        superseded = sum(1 for n in cat_nodes if n.get("status") == "superseded")
        cards = "".join(_render_browse_card(n, question_refs) for n in cat_nodes)
        groups.append(
            f'<section class="category" data-category="{_esc(category)}">'
            f'<h3 class="category-head">{_esc(_category_label(category))}'
            f"{_category_count_badge(active, superseded)}</h3>"
            f'<div class="card-grid">{cards}</div></section>'
        )

    return (
        '<main class="view view-browse" data-view="browse">'
        '<p class="section-note">Every decision grouped by category; superseded '
        "decisions are dimmed. Use the status filter to focus active or "
        "superseded. Select a decision to expand its relations and linked "
        "questions.</p>"
        f"{''.join(groups)}"
        '<p class="empty-filter" hidden>No decisions match the current filters.</p>'
        "</main>"
    )


def _category_count_badge(active: int, superseded: int) -> str:
    """Truthful per-group count badge: active alone, or active and superseded.

    The badge carries data attributes so the script can rewrite it to the
    filtered counts when the status filter narrows the group.
    """
    if superseded:
        text = f"{active} active · {superseded} superseded"
    else:
        text = f"{active} active"
    return (
        f'<span class="category-count" data-active="{active}" '
        f'data-superseded="{superseded}">{text}</span>'
    )


def _render_browse_card(node: dict, question_refs: dict[int, list[str]]) -> str:
    """Render one decision as an expandable Browse card.

    The title wraps in full. Date, status, and confidence render quietly as
    secondary metadata; a superseded card is visibly distinct. The card toggles
    the shared detail panel for this node.
    """
    number = node.get("number", 0)
    title = node.get("title", "")
    date = node.get("date", "")
    confidence = node.get("confidence", "")
    status = node.get("status", "active")
    question_badge = _question_badge(number, question_refs)
    status_mark = "○ superseded" if status == "superseded" else "● active"
    return (
        f'<article class="card status-{_esc(status)}" data-number="{number}" '
        f'data-title="{_esc(title)}" data-status="{_esc(status)}" '
        f'data-category="{_esc(_category_of(node.get("decision_type")))}" '
        f'data-confidence="{_esc(confidence)}" '
        f'tabindex="0" role="button" data-detail-trigger="{number}">'
        f'<span class="card-id">D{number} '
        f'<span class="card-status">{_esc(status_mark)}</span></span>'
        f'<span class="card-title">{_esc(title)}</span>'
        f'<span class="card-meta">{_esc(date)} · {_esc(confidence)} confidence</span>'
        f"{question_badge}"
        "</article>"
    )


def _question_badge(number: int, question_refs: dict[int, list[str]]) -> str:
    """Render the open-question badge for a decision, or empty when none.

    The badge links back to the questions section and names the count, so a
    referenced decision visibly carries its open threads.
    """
    qids = question_refs.get(number)
    if not qids:
        return ""
    plural = "question" if len(qids) == 1 else "questions"
    return (
        f'<a class="q-badge" href="#questions" data-q-badge="{number}">'
        f"{len(qids)} open {plural}</a>"
    )


# ── View B: Lineage ──


def _render_lineage_view(payload: dict, relations: dict[int, dict[str, list[int]]]) -> str:
    """Render the Lineage view: one drawn DAG per supersession component.

    Components sort by size (largest first) then recency (latest member date).
    Singletons never reach here (the builder only emits components with edges).
    """
    components = payload.get("components", [])
    if not components:
        return (
            '<main class="view view-lineage" data-view="lineage">'
            '<p class="section-note">No supersession threads yet. Every decision '
            "stands on its own.</p></main>"
        )

    node_by_number = {n["number"]: n for n in payload.get("nodes", [])}
    ordered = _order_components(components, node_by_number)

    blocks: list[str] = []
    for component in ordered:
        blocks.append(_render_lineage_component(component, node_by_number, relations))

    return (
        '<main class="view view-lineage" data-view="lineage">'
        '<p class="section-note">Each thread is a supersession DAG; time flows '
        "left to right, oldest generation at the left. Edges run from a retired "
        "decision to the decision that retired it. A fan-in converging on one "
        "active decision is a consolidation.</p>"
        f"{''.join(blocks)}"
        "</main>"
    )


def _order_components(components: list[dict], node_by_number: dict[int, dict]) -> list[dict]:
    """Sort components by node count descending, then by latest member date.

    The payload already orders by size then smallest member; this re-sorts the
    secondary key to recency (the most recent decision in the thread) so newer
    consolidations surface above equally sized older ones.
    """

    def sort_key(component: dict) -> tuple[int, str]:
        dates = [node_by_number.get(n, {}).get("date", "") for n in component["nodes"]]
        latest = max(dates) if dates else ""
        # Size descending (negated), then recency descending: a higher latest
        # date should sort earlier, so invert the string via its complement is
        # not available; instead sort size asc/recency asc then reverse the
        # whole list would break the size tiebreak. Use a two-pass sort instead.
        return (-len(component["nodes"]), latest)

    # Stable two-key sort: primary size descending, secondary recency
    # descending. Python sorts ascending, so apply recency first (descending)
    # then size (descending) using stability to preserve recency order within a
    # size band.
    by_recency = sorted(components, key=lambda c: sort_key(c)[1], reverse=True)
    return sorted(by_recency, key=lambda c: -len(c["nodes"]))


def _lineage_columns(component: dict) -> dict[int, int]:
    """Assign each node a generation column = longest distance from a root.

    A root is a node with no incoming supersession edge inside the component
    (nothing supersedes it; it is the oldest generation). The column index is
    the longest path in edges from any root to the node along supersession
    direction ``retired -> retirer`` reversed into generation order: an edge
    ``(from, to)`` means ``from`` supersedes ``to``, so ``to`` is older and sits
    in an earlier column than ``from``.

    The longest-distance layout is computed over the DAG formed by treating
    "older" as the source. Cycles (a malformed back-and-forth pair) are bounded
    by a visited guard so the relaxation terminates.
    """
    nodes = component["nodes"]
    edges = component["edges"]
    # Build the older -> newer adjacency: edge (from, to) means from supersedes
    # to, so to is older; the generation edge points older (to) -> newer (from).
    newer_of: dict[int, list[int]] = {n: [] for n in nodes}
    for e in edges:
        older, newer = e["to"], e["from"]
        newer_of[older].append(newer)

    # Longest-path column by relaxing every generation edge each pass. A root
    # (nothing older points to it) keeps column 0. The longest chain has at most
    # len(nodes) - 1 edges, so len(nodes) passes converge; the early break ends
    # sooner, and a malformed cycle still terminates at the pass bound.
    column = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for older in nodes:
            for newer in newer_of[older]:
                if column[newer] < column[older] + 1:
                    column[newer] = column[older] + 1
                    changed = True
        if not changed:
            break
    return column


def _barycentric_rows(
    nodes: list[int],
    edges: list[dict],
    columns: dict[int, int],
) -> dict[int, int]:
    """Assign each node a row slot so a fan-in radiates into its retirer.

    Column 0 (the oldest generation, the retired leaves) keeps rows ascending by
    number. For every later column the row is the mean of the rows of the nodes
    the node supersedes (its predecessors one column to the left), so a retirer
    sits vertically centered on the children that converge into it: D297 lands
    centered on its 13 leaves, D208 centered between the D105 chain and D73.

    Collisions within a column are resolved deterministically. Nodes are ordered
    by ``(barycenter, number)`` and then walked top to bottom, pushing each down
    to at least one slot below the previous so no two share a slot and none
    overlap. The push only ever increases a row, so the relative order from the
    barycenter sort is preserved and the result is independent of input order.

    Returns integer row slots; the caller multiplies by the row pitch.
    """
    # Predecessors of a node = the nodes it supersedes (edge from this node).
    supersedes: dict[int, list[int]] = {n: [] for n in nodes}
    for e in edges:
        supersedes[e["from"]].append(e["to"])

    by_column: dict[int, list[int]] = {}
    for n in nodes:
        by_column.setdefault(columns[n], []).append(n)

    row: dict[int, int] = {}
    max_col = max(columns.values(), default=0)

    # Column 0: ascending by number, one slot each.
    col0 = sorted(by_column.get(0, []))
    for slot, num in enumerate(col0):
        row[num] = slot

    for col_index in range(1, max_col + 1):
        members = by_column.get(col_index, [])
        # Barycenter from already-placed predecessors (all sit in lower columns
        # because columns are the longest distance from a root). A node with no
        # placed predecessor falls back to its own number so it stays
        # deterministic and clusters near similarly numbered peers.
        barycenter: dict[int, float] = {}
        for num in members:
            preds = [p for p in supersedes[num] if p in row]
            if preds:
                barycenter[num] = sum(row[p] for p in preds) / len(preds)
            else:
                barycenter[num] = float(num)
        ordered = sorted(members, key=lambda n: (barycenter[n], n))
        last_slot: float | None = None
        for num in ordered:
            target = round(barycenter[num])
            if last_slot is not None and target <= last_slot:
                target = last_slot + 1
            row[num] = target
            last_slot = target

    return row


def _render_lineage_component(
    component: dict,
    node_by_number: dict[int, dict],
    relations: dict[int, dict[str, list[int]]],
) -> str:
    """Render one component as an inline SVG DAG with drawn edges.

    Column index is the longest distance from the root(s). Rows use barycentric
    placement: column-0 leaves ascend by number, and every retirer sits at the
    mean row of the children it supersedes, so a fan-in radiates into its
    retirer rather than running as parallel hairlines. Edges are drawn as SVG
    paths from each retired node to its retirer; edges into a consolidation
    target carry heavier weight and the emphasis color so the fan is the most
    prominent drawn object.
    """
    columns = _lineage_columns(component)
    nodes = component["nodes"]
    branch_points = set(component.get("branch_points", []))

    rows = _barycentric_rows(nodes, component["edges"], columns)
    max_col = max(columns.values(), default=0)
    max_row = max(rows.values(), default=0)

    position: dict[int, tuple[float, float]] = {}
    for num in nodes:
        x = _MARGIN_X + columns[num] * _COL_WIDTH
        y = _MARGIN_Y + rows[num] * _ROW_PITCH
        position[num] = (x, y)

    width = _MARGIN_X * 2 + max_col * _COL_WIDTH + _NODE_WIDTH
    height = _MARGIN_Y * 2 + max_row * _ROW_PITCH + _NODE_HEIGHT

    # Consolidation targets: active retirers that fan in three or more children.
    # Edges into one carry the emphasis class so the converging bundle is the
    # most prominent drawn object on the page.
    consolidation: set[int] = {
        num
        for num in nodes
        if len(relations.get(num, {}).get("supersedes", [])) >= 3
        and node_by_number.get(num, {}).get("status") == "active"
    }

    # Edge paths: from retired (to) on the left toward retirer (from) on the
    # right. The path leaves the right edge of the older node and enters the
    # left edge of the newer node so a fan-in reads as a converging bundle.
    edge_paths: list[str] = []
    for e in component["edges"]:
        older = e["to"]
        newer = e["from"]
        ox, oy = position[older]
        nx, ny = position[newer]
        x1 = ox + _NODE_WIDTH
        y1 = oy + _NODE_HEIGHT / 2
        x2 = nx
        y2 = ny + _NODE_HEIGHT / 2
        midx = (x1 + x2) / 2
        path = f"M {x1:.1f} {y1:.1f} C {midx:.1f} {y1:.1f} {midx:.1f} {y2:.1f} {x2:.1f} {y2:.1f}"
        edge_class = "edge consolidation-edge" if newer in consolidation else "edge"
        edge_paths.append(
            f'<path class="{edge_class}" d="{path}" data-from="{newer}" data-to="{older}" />'
        )

    # Node rectangles. The target of a fan-in (a branch point that is active) is
    # emphasized as the consolidation anchor.
    node_svg: list[str] = []
    for num in nodes:
        node = node_by_number.get(num, {})
        x, y = position[num]
        status = node.get("status", "active")
        fan_in_size = len(relations.get(num, {}).get("supersedes", []))
        classes = ["lnode", f"status-{_esc(status)}"]
        if num in branch_points:
            classes.append("branch")
        if num in consolidation:
            classes.append("consolidation")
        title = node.get("title", "")
        label = title if len(title) <= 28 else title[:27].rstrip() + "…"
        emphasis = f' data-fanin="{fan_in_size}"' if fan_in_size >= 3 else ""
        node_svg.append(
            f'<g class="{" ".join(classes)}" data-number="{num}" '
            f'data-detail-trigger="{num}" tabindex="0" role="button"{emphasis} '
            f'transform="translate({x:.1f},{y:.1f})">'
            f'<rect width="{_NODE_WIDTH}" height="{_NODE_HEIGHT}" rx="4" />'
            f'<text class="lnode-id" x="8" y="20">D{num}</text>'
            f'<text class="lnode-title" x="8" y="38">{_esc(label)}</text>'
            "</g>"
        )

    size = len(nodes)
    headline = _component_headline(size, sorted(consolidation), relations)
    return (
        '<section class="thread" data-size="{size}">'
        '<h3 class="thread-head">{headline}</h3>'
        '<div class="thread-canvas">'
        '<svg class="lineage-svg" viewBox="0 0 {w} {h}" '
        'preserveAspectRatio="xMinYMin meet" width="{w}" height="{h}">'
        '<g class="edges">{edges}</g>'
        '<g class="nodes">{nodes}</g>'
        "</svg></div></section>"
    ).format(
        size=size,
        headline=headline,
        w=int(width),
        h=int(height),
        edges="".join(edge_paths),
        nodes="".join(node_svg),
    )


def _component_headline(
    size: int, fan_targets: list[int], relations: dict[int, dict[str, list[int]]]
) -> str:
    """Headline for a lineage thread, naming the consolidation when present."""
    if fan_targets:
        biggest = max(fan_targets, key=lambda n: len(relations[n]["supersedes"]))
        count = len(relations[biggest]["supersedes"])
        return f"Thread of {size} decisions · D{biggest} consolidates {count}"
    return f"Thread of {size} decisions"


# ── View C: Timeline ──


def _render_timeline_view(payload: dict) -> str:
    """Render the Timeline view: marks positioned by real date on a date axis.

    The X axis runs from the earliest to the latest decision date. Category
    lanes stack vertically. Marks are positioned by their date's fraction of the
    full span, so a cluster of dates reads as a visible cluster and a single
    busy date reads as a stack. Active and superseded differ by color and
    weight. This is a real axis, never a grid pretending to be one.
    """
    nodes = payload.get("nodes", [])
    dates = sorted(_to_ordinal(n.get("date", "")) for n in nodes if n.get("date"))
    if not dates:
        return (
            '<main class="view view-timeline" data-view="timeline">'
            '<p class="section-note">No dated decisions to plot.</p></main>'
        )
    first, last = dates[0], dates[-1]
    single_day = first == last
    # A real span of zero means every decision shares one date. Rather than
    # invent a fake next-day tick, center the marks and draw a single tick.
    span = last - first if not single_day else 1

    categories = _present_categories(nodes)
    lane_index = {c: i for i, c in enumerate(categories)}

    # Per-lane height accommodates that lane's deepest (date, lane) stack so a
    # busy day never spills into the next lane. Lane tops accumulate downward.
    deepest = _lane_stack_depth(nodes, lane_index)
    lane_height = {
        c: max(_TL_LANE_HEIGHT, deepest[c] * _TL_STACK_STEP + _TL_LANE_HEIGHT * 0.6)
        for c in categories
    }
    lane_top: dict[str, float] = {}
    y_cursor = _TL_TOP_PAD
    for c in categories:
        lane_top[c] = y_cursor
        y_cursor += lane_height[c]
    plot_height = y_cursor + 28
    total_width = _TL_LEFT_GUTTER + _TL_PLOT_WIDTH + _TL_RIGHT_PAD

    axis = _timeline_axis(first, last, span, plot_height, single_day)
    lanes = "".join(_timeline_lane_label(c, lane_top[c], lane_height[c]) for c in categories)

    marks: list[str] = []
    # Deterministic vertical stacking for marks that share a (ordinal, lane)
    # cell: the nth mark in that cell drops by n steps from the lane top, so a
    # busy day reads as a visible column instead of one hidden overlap. Iteration
    # is in sorted (date, number) order, so the stack order is reproducible.
    stack_index: dict[tuple[int, str], int] = {}
    for node in sorted(nodes, key=lambda n: (n.get("date", ""), n.get("number", 0))):
        date = node.get("date", "")
        if not date:
            continue
        category = _category_of(node.get("decision_type"))
        ordinal = _to_ordinal(date)
        # Single-day stores center every mark; otherwise place by date fraction.
        frac = 0.5 if single_day else (ordinal - first) / span
        cx = _TL_LEFT_GUTTER + frac * _TL_PLOT_WIDTH
        cell = (ordinal, category)
        offset = stack_index.get(cell, 0)
        stack_index[cell] = offset + 1
        cy = lane_top[category] + _TL_LANE_HEIGHT * 0.4 + offset * _TL_STACK_STEP
        status = node.get("status", "active")
        r = 6 if status == "active" else 4
        number = node.get("number", 0)
        title = node.get("title", "")
        confidence = node.get("confidence", "medium")
        marks.append(
            f'<circle class="tl-mark status-{_esc(status)}" cx="{cx:.1f}" cy="{cy:.1f}" '
            f'r="{r}" data-number="{number}" data-date="{_esc(date)}" '
            f'data-status="{_esc(status)}" data-category="{_esc(category)}" '
            f'data-confidence="{_esc(confidence)}" data-title="{_esc(title)}" '
            f'data-detail-trigger="{number}" tabindex="0" role="button">'
            f"<title>D{number} · {_esc(date)}</title></circle>"
        )

    return (
        '<main class="view view-timeline" data-view="timeline">'
        '<p class="section-note">Decisions on a true date axis from the earliest '
        "to the latest decision. Lanes are categories; larger marks are active, "
        "smaller marks superseded. Marks sharing a day in a lane stack so a busy "
        "day reads as a column.</p>"
        '<div class="timeline-canvas">'
        f'<svg class="timeline-svg" viewBox="0 0 {total_width} {plot_height}" '
        f'preserveAspectRatio="xMinYMin meet" width="{total_width}" height="{plot_height}">'
        f'<g class="tl-axis">{axis}</g>'
        f'<g class="tl-lanes">{lanes}</g>'
        f'<g class="tl-marks">{"".join(marks)}</g>'
        "</svg></div></main>"
    )


def _lane_stack_depth(nodes: list[dict], lane_index: dict[str, int]) -> dict[str, int]:
    """Deepest count of marks sharing a (date, lane) cell, per lane.

    Drives each lane's height so its busiest day's stack fits without spilling
    into the neighbouring lane.
    """
    counts: dict[tuple[str, str], int] = {}
    for node in nodes:
        date = node.get("date", "")
        if not date:
            continue
        category = _category_of(node.get("decision_type"))
        if category not in lane_index:
            continue
        key = (date, category)
        counts[key] = counts.get(key, 0) + 1
    deepest: dict[str, int] = {c: 1 for c in lane_index}
    for (_, category), count in counts.items():
        if count > deepest[category]:
            deepest[category] = count
    return deepest


def _timeline_axis(first: int, last: int, span: int, plot_height: float, single_day: bool) -> str:
    """Build axis baseline and date tick labels for the timeline.

    A single-day store gets exactly one centered tick at its only date; it never
    invents a second date. Otherwise ticks are first, last, and evenly spaced
    interior dates.
    """
    baseline_y = plot_height - 14
    if single_day:
        x = _TL_LEFT_GUTTER + 0.5 * _TL_PLOT_WIDTH
        label = _ordinal_to_iso(first)
        return (
            f'<line class="tl-tick" x1="{x:.1f}" y1="{_TL_TOP_PAD - 12}" '
            f'x2="{x:.1f}" y2="{baseline_y:.1f}" />'
            f'<text class="tl-tick-label" x="{x:.1f}" y="{baseline_y + 14:.1f}" '
            f'text-anchor="middle">{_esc(label)}</text>'
        )
    ticks: list[str] = []
    # First and last always; interior ticks evenly spaced by ordinal.
    tick_count = 5
    seen: set[int] = set()
    for i in range(tick_count + 1):
        ordinal = first + round(span * i / tick_count)
        if ordinal in seen:
            continue
        seen.add(ordinal)
        frac = (ordinal - first) / span
        x = _TL_LEFT_GUTTER + frac * _TL_PLOT_WIDTH
        label = _ordinal_to_iso(ordinal)
        ticks.append(
            f'<line class="tl-tick" x1="{x:.1f}" y1="{_TL_TOP_PAD - 12}" '
            f'x2="{x:.1f}" y2="{baseline_y:.1f}" />'
            f'<text class="tl-tick-label" x="{x:.1f}" y="{baseline_y + 14:.1f}" '
            f'text-anchor="middle">{_esc(label)}</text>'
        )
    return "".join(ticks)


def _timeline_lane_label(category: str, top: float, height: float) -> str:
    """Render a category lane label and separator for the timeline."""
    cy = top + height / 2
    return (
        f'<line class="tl-lane-rule" x1="{_TL_LEFT_GUTTER}" y1="{top:.1f}" '
        f'x2="{_TL_LEFT_GUTTER + _TL_PLOT_WIDTH}" y2="{top:.1f}" />'
        f'<text class="tl-lane-label" x="12" y="{cy + 4:.1f}">'
        f"{_esc(_category_label(category))}</text>"
    )


def _to_ordinal(iso_date: str) -> int:
    """Convert an ISO ``YYYY-MM-DD`` date to its proleptic-Gregorian day ordinal.

    Uses ``datetime.date.toordinal`` so the spacing is calendar-exact: a one-day
    gap is exactly one unit, a leap day counts, and month lengths are real. This
    is date arithmetic on stored data, not a clock read, so it does not break the
    builder's no-clock rule (the renderer is the consumer here, and the date is
    a payload value). A malformed date returns 0 so a bad entry cannot crash the
    render.
    """
    parts = iso_date.split("-")
    if len(parts) != 3:
        return 0
    try:
        return _date(int(parts[0]), int(parts[1]), int(parts[2])).toordinal()
    except ValueError:
        return 0


def _ordinal_to_iso(ordinal: int) -> str:
    """Inverse of ``_to_ordinal`` for tick labels, exact via fromordinal."""
    if ordinal <= 0:
        return ""
    return _date.fromordinal(ordinal).isoformat()


# ── Shared detail store ──


def _render_detail_store(
    payload: dict,
    relations: dict[int, dict[str, list[int]]],
    question_refs: dict[int, list[str]],
) -> str:
    """Render one hidden detail block per node, reused by every view.

    Each block holds the node's relations (supersedes, superseded by, cited by)
    as chips that jump to the related node in the Graph view (falling back to its
    detail panel), plus the open questions that reference the node. The floating
    panel surfaces the matching block when a node is selected, so Graph, Lineage,
    Timeline, and Browse open the same panel.
    """
    cited_by = _citation_map(payload)
    bodies_present = any("body" in n for n in payload.get("nodes", []))

    blocks: list[str] = []
    for node in payload.get("nodes", []):
        number = node["number"]
        rel = relations.get(number, {"supersedes": [], "superseded_by": []})
        chips = _relation_chips(rel, cited_by.get(number, []))
        question_links = _detail_question_links(number, question_refs)
        body_block = _detail_body(node) if bodies_present else ""
        status = node.get("status", "active")
        blocks.append(
            f'<section class="detail" data-detail="{number}" hidden>'
            f'<header class="detail-head">'
            f'<span class="detail-id">D{number}</span>'
            f'<span class="detail-status status-{_esc(status)}">{_esc(status)}</span>'
            f'<span class="detail-meta">{_esc(node.get("date", ""))} · '
            f"{_esc(node.get('confidence', ''))} confidence · "
            f"{_esc(_category_label(_category_of(node.get('decision_type'))))}</span>"
            "</header>"
            f'<p class="detail-title">{_esc(node.get("title", ""))}</p>'
            f"{chips}{question_links}{body_block}"
            "</section>"
        )
    return f'<div id="detail-store" hidden>{"".join(blocks)}</div>'


def _relation_chips(rel: dict[str, list[int]], cited_by: list[int]) -> str:
    """Render relation chips that jump to the related node in the Graph view."""
    rows: list[str] = []
    if rel.get("supersedes"):
        rows.append(_chip_row("Supersedes", rel["supersedes"]))
    if rel.get("superseded_by"):
        rows.append(_chip_row("Superseded by", rel["superseded_by"]))
    if cited_by:
        rows.append(_chip_row("Cited by", cited_by))
    if not rows:
        return '<p class="detail-empty">No relations. This decision stands alone.</p>'
    return f'<div class="detail-relations">{"".join(rows)}</div>'


def _chip_row(label: str, numbers: list[int]) -> str:
    """Render one labelled row of clickable relation chips."""
    chips = "".join(f'<button class="chip" data-jump="{n}">D{n}</button>' for n in numbers)
    return f'<div class="chip-row"><span class="chip-label">{_esc(label)}</span>{chips}</div>'


def _detail_question_links(number: int, question_refs: dict[int, list[str]]) -> str:
    """Render the open questions that reference this decision."""
    qids = question_refs.get(number)
    if not qids:
        return ""
    links = "".join(
        f'<a class="q-link" href="#questions" data-q-link="{_esc(qid)}">{_esc(qid)}</a>'
        for qid in qids
    )
    return (
        f'<div class="detail-questions"><span class="chip-label">Open questions</span>{links}</div>'
    )


def _detail_body(node: dict) -> str:
    """Render the decision body behind an expander when bodies are included."""
    body = node.get("body")
    if not body:
        return ""
    return (
        '<details class="detail-body"><summary>Decision body</summary>'
        f'<pre class="body-text">{_esc(body)}</pre></details>'
    )


# ── Questions section ──


def _render_questions_section(payload: dict) -> str:
    """Render the integrated open-questions list.

    Open questions sort first (the builder already filters to unresolved). Long
    bodies collapse behind an expander. Decision references render as links into
    the detail panel; the inverse badge sits on each referenced decision's
    detail and Browse card.
    """
    questions = payload.get("open_questions", [])
    if not questions:
        return (
            '<section id="questions" class="questions"><h2>Open questions</h2>'
            '<p class="section-note">No open questions.</p></section>'
        )

    items: list[str] = []
    for q in questions:
        qid = q.get("id", "")
        body = q.get("body", "")
        refs = q.get("references", [])
        ref_links = "".join(f'<button class="q-ref" data-jump="{n}">D{n}</button>' for n in refs)
        ref_block = (
            f'<span class="q-refs"><span class="chip-label">references</span>{ref_links}</span>'
            if refs
            else ""
        )
        long = len(body) > 160
        if long:
            head = _esc(body[:157].rstrip()) + "…"
            body_html = (
                f'<details class="q-expand"><summary class="q-body">{head}</summary>'
                f'<p class="q-full">{_esc(body)}</p></details>'
            )
        else:
            body_html = f'<span class="q-body">{_esc(body)}</span>'
        items.append(
            f'<li class="question" id="question-{_esc(qid)}" data-q-id="{_esc(qid)}">'
            f'<span class="q-id">{_esc(qid)}</span>{body_html}{ref_block}</li>'
        )

    return (
        '<section id="questions" class="questions"><h2>Open questions</h2>'
        f'<ul class="question-list">{"".join(items)}</ul></section>'
    )


# ── Empty state and footer ──


def _render_empty_state() -> str:
    """Render the intentional empty state for a store with no decisions.

    Sits in the Graph view container so the default tab is consistent with a
    populated store; there is simply nothing to draw yet.
    """
    return (
        '<main class="view view-graph is-active" data-view="graph">'
        '<section class="empty-state">'
        "<h2>No decisions yet</h2>"
        "<p>Record the first decision with <code>nauro note</code> or your "
        "agent's propose-decision tool, then run this command again.</p>"
        "</section></main>"
    )


def _render_footer(payload: dict, generated_at: str) -> str:
    """Render the footer naming the project, decision count, and generation time."""
    project = payload.get("project") or "this project"
    decision_count = payload.get("decision_count", 0)
    plural = "decision" if decision_count == 1 else "decisions"
    return (
        f'<footer class="page-footer">{_esc(project)} · '
        f"{decision_count} {plural} · generated {_esc(generated_at)}</footer>"
    )


_STYLES = """
:root {
  --paper: #F5F0E5;
  --ink: #1a1915;
  --navy: #0F3A52;
  --accent: #8a3520;
  --line: #d8cfbd;
  --muted: #6b6557;
  --panel: #fbf8f0;
  --consol: #b4471f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #14181b;
    --ink: #e7e2d6;
    --navy: #4fb6c4;
    --accent: #c8745f;
    --line: #2a3036;
    --muted: #9a978c;
    --panel: #1b2024;
    --consol: #e08a5f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 1.5rem 4rem;
  background: var(--paper);
  color: var(--ink);
  font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
header.page-header {
  border-bottom: 2px solid var(--navy);
  padding: 1.2rem 0 0.8rem;
  margin-bottom: 1.2rem;
  position: sticky;
  top: 0;
  background: var(--paper);
  z-index: 5;
}
header.page-header h1 {
  margin: 0 0 0.5rem;
  font-size: 1.5rem;
  color: var(--navy);
  font-weight: 600;
}
.stat-row { display: flex; flex-wrap: wrap; gap: 1.4rem; margin-bottom: 0.7rem; }
.stat { font-size: 0.92rem; color: var(--muted); }
.stat strong { color: var(--ink); font-size: 1.05rem; }
.view-tabs { display: flex; gap: 0.4rem; margin-bottom: 0.7rem; }
.view-tab {
  font: inherit;
  font-size: 0.92rem;
  padding: 0.35rem 0.9rem;
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--muted);
  border-radius: 4px;
  cursor: pointer;
}
.view-tab.is-active { color: var(--paper); background: var(--navy); border-color: var(--navy); }
.controls { display: flex; flex-wrap: wrap; gap: 1rem; align-items: flex-end; font-size: 0.85rem; }
.controls label { display: flex; flex-direction: column; gap: 0.2rem; color: var(--muted); }
.controls input, .controls select {
  font: inherit;
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--ink);
  border-radius: 3px;
}
.controls .search input { min-width: 16rem; }
h2 {
  color: var(--navy);
  font-size: 1.15rem;
  border-bottom: 1px solid var(--line);
  padding-bottom: 0.3rem;
}
.section-note { color: var(--muted); font-size: 0.85rem; max-width: 72ch; }
.view { display: none; }
.view.is-active { display: block; }
/* Graph canvas: always dark, like a map panel, in both themes. */
.graph-canvas {
  background: #11161b;
  border: 1px solid #2a3036;
  border-radius: 6px;
  height: 78vh;
  min-height: 520px;
  overflow: hidden;
  position: relative;
}
.graph-svg { display: block; width: 100%; height: 100%; cursor: grab; }
.graph-svg.is-panning { cursor: grabbing; }
.graph-note { color: var(--muted); font-size: 0.82rem; margin: 0.5rem 0 0; max-width: 72ch; }
/* Story strip: a compact docent row above the canvas. */
.story-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 0 0 0.6rem;
}
.story-card {
  font: inherit;
  text-align: left;
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 5px;
  padding: 0.35rem 0.7rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  min-width: 0;
}
.story-card:hover, .story-card:focus { border-color: var(--navy); outline: none; }
.story-kicker {
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
}
.story-body { font-size: 0.85rem; color: var(--ink); }
.cite-edge { stroke: #6f8aa0; stroke-width: 1; opacity: 0.09; }
.cite-edge.incident { stroke: #a9c5db; opacity: 0.55; stroke-width: 1.4; }
.cite-edge.edge-dim { opacity: 0.015; }
.sup-edge { stroke: #d2693f; stroke-width: 1.8; opacity: 0.72; }
.sup-edge.consolidation-edge { stroke: #e8895c; stroke-width: 3.2; opacity: 0.95; }
.sup-edge.incident { opacity: 1; }
.sup-edge.edge-dim { opacity: 0.12; }
.gnode { stroke: #11161b; stroke-width: 1.2; cursor: pointer; }
.gnode.status-superseded { fill-opacity: 0.18 !important; stroke-width: 2; }
.gnode.is-hub { stroke: #f2ead8; stroke-width: 1.6; }
.gnode.match { stroke: #ffd56b; stroke-width: 2.6; }
.gnode.dim { opacity: 0.12; }
.gnode.flash { stroke: #ffd56b; stroke-width: 3.4; }
.gnode.date-hit { stroke: #7fd6a0; stroke-width: 3; }
.gnode-label {
  fill: #f2ead8;
  font-size: 12px;
  font-weight: 600;
  paint-order: stroke;
  stroke: #11161b;
  stroke-width: 3px;
  pointer-events: none;
}
.gnode-label.dim { opacity: 0.1; }
.gnode-label.show { opacity: 1; }
.disc-label {
  fill: #9fb0bd;
  font-size: 13px;
  letter-spacing: 0.04em;
  pointer-events: none;
}
.category { margin-bottom: 1.6rem; }
.category-head {
  color: var(--accent);
  font-size: 1rem;
  margin: 0.4rem 0 0.6rem;
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}
.category-count {
  font-size: 0.78rem;
  color: var(--muted);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.05rem 0.5rem;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr));
  gap: 0.7rem;
}
.card {
  border: 1px solid var(--line);
  border-left: 3px solid var(--navy);
  background: var(--panel);
  padding: 0.6rem 0.7rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  cursor: pointer;
}
.card:hover, .card:focus { border-left-width: 5px; outline: none; }
.card.status-superseded {
  border-left-color: var(--accent);
  border-left-style: dashed;
  opacity: 0.62;
}
.card-id { font-weight: 700; color: var(--navy); font-size: 0.82rem; }
.card.status-superseded .card-id { color: var(--accent); }
.card-status {
  font-weight: 400;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
}
.card.status-superseded .card-status { color: var(--accent); }
.card-title { font-size: 0.95rem; line-height: 1.35; }
.card-meta { color: var(--muted); font-size: 0.76rem; }
.q-badge {
  align-self: flex-start;
  font-size: 0.72rem;
  color: var(--accent);
  text-decoration: none;
  border: 1px solid var(--accent);
  border-radius: 999px;
  padding: 0.02rem 0.5rem;
}
.empty-filter { color: var(--muted); font-style: italic; }
.thread { margin-bottom: 1.6rem; }
.thread-head { color: var(--navy); font-size: 1rem; margin: 0.5rem 0; }
.thread-canvas {
  overflow-x: auto;
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 0.5rem;
}
.lineage-svg { display: block; }
.edge { fill: none; stroke: var(--ink); stroke-width: 2; opacity: 0.55; }
.consolidation-edge { stroke: var(--navy); stroke-width: 3; opacity: 0.85; }
.lnode rect { fill: var(--paper); stroke: var(--navy); stroke-width: 1.5; cursor: pointer; }
.lnode.status-superseded rect { stroke: var(--accent); stroke-dasharray: 4 3; }
.lnode.consolidation rect { stroke: var(--consol); stroke-width: 3.5; fill: var(--panel); }
.lnode-id { font-size: 12px; font-weight: 700; fill: var(--navy); }
.lnode.status-superseded .lnode-id { fill: var(--accent); }
.lnode.consolidation .lnode-id { fill: var(--consol); }
.lnode-title { font-size: 11px; fill: var(--ink); }
.timeline-canvas {
  overflow-x: auto;
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 0.5rem;
}
.timeline-svg { display: block; }
.tl-lane-rule { stroke: var(--line); stroke-width: 1; }
.tl-lane-label { font-size: 11px; fill: var(--muted); }
.tl-tick { stroke: var(--line); stroke-width: 1; stroke-dasharray: 2 3; }
.tl-tick-label { font-size: 10px; fill: var(--muted); }
.tl-mark { cursor: pointer; }
.tl-mark.status-active { fill: var(--navy); }
.tl-mark.status-superseded { fill: var(--accent); opacity: 0.7; }
.questions { margin-top: 2rem; }
.question-list { list-style: none; padding: 0; }
.question {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: baseline;
  padding: 0.4rem 0;
  border-bottom: 1px solid var(--line);
}
.q-id { font-weight: 700; color: var(--navy); }
.q-body { flex: 1; min-width: 18rem; }
.q-expand { flex: 1; min-width: 18rem; }
.q-expand summary { cursor: pointer; }
.q-full { margin: 0.4rem 0 0; color: var(--ink); }
.q-refs, .detail-questions { display: flex; gap: 0.3rem; align-items: center; flex-wrap: wrap; }
.chip-label {
  font-size: 0.72rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.q-ref, .chip {
  font: inherit;
  font-size: 0.78rem;
  border: 1px solid var(--navy);
  color: var(--navy);
  background: transparent;
  border-radius: 3px;
  padding: 0.02rem 0.4rem;
  cursor: pointer;
}
.q-link {
  font-size: 0.8rem;
  color: var(--accent);
  text-decoration: none;
  border: 1px solid var(--accent);
  border-radius: 3px;
  padding: 0.02rem 0.4rem;
}
#detail-store { display: none; }
.detail-panel {
  position: fixed;
  top: 0;
  right: 0;
  width: min(30rem, 92vw);
  height: 100vh;
  background: var(--panel);
  border-left: 2px solid var(--navy);
  box-shadow: -8px 0 24px rgba(0,0,0,0.18);
  padding: 1.2rem 1.3rem;
  overflow-y: auto;
  z-index: 20;
  transform: translateX(100%);
  transition: transform 0.18s ease;
}
.detail-panel.is-open { transform: translateX(0); }
.detail-panel-close {
  position: absolute;
  top: 0.7rem;
  right: 0.9rem;
  font: inherit;
  font-size: 1.2rem;
  border: none;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.detail { display: block; }
.detail-head { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: baseline; }
.detail-id { font-weight: 700; color: var(--navy); font-size: 1.1rem; }
.detail-status {
  font-size: 0.74rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
}
.detail-status.status-superseded { color: var(--accent); }
.detail-meta { font-size: 0.78rem; color: var(--muted); }
.detail-title { font-size: 1.05rem; margin: 0.6rem 0 0.8rem; line-height: 1.4; }
.detail-relations { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 0.8rem; }
.chip-row { display: flex; gap: 0.3rem; align-items: center; flex-wrap: wrap; }
.detail-empty { color: var(--muted); font-style: italic; }
.detail-body { margin-top: 0.8rem; }
.detail-body summary { cursor: pointer; color: var(--navy); }
.body-text {
  white-space: pre-wrap;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 0.78rem;
  background: var(--paper);
  border: 1px solid var(--line);
  padding: 0.6rem;
  border-radius: 4px;
  overflow-x: auto;
}
.dimmed { opacity: 0.18; }
.question.flash, li.flash, .q-expand.flash { background: var(--line); }
code {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  background: var(--line);
  padding: 0.05rem 0.3rem;
  border-radius: 3px;
  font-size: 0.85em;
}
.empty-state { padding: 3rem 0; max-width: 50ch; }
.empty-state h2 { border: none; }
.page-footer {
  margin-top: 3rem;
  padding-top: 1rem;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 0.8rem;
}
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
"""


_SCRIPT = """
(function () {
  // The page is driven entirely by the server-rendered DOM (data attributes on
  // cards, SVG nodes, edges, and the detail store). The embedded JSON block is
  // the verbatim payload kept for inspection and a future hosted renderer;
  // nothing here reads it, so there is no client-side reference parsing.

  var views = {};
  var tabs = document.querySelectorAll(".view-tab");
  var viewEls = document.querySelectorAll(".view");
  for (var v = 0; v < viewEls.length; v++) {
    views[viewEls[v].getAttribute("data-view")] = viewEls[v];
  }

  function showView(name) {
    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      t.classList.toggle("is-active", t.getAttribute("data-view") === name);
    }
    for (var k in views) {
      if (Object.prototype.hasOwnProperty.call(views, k)) {
        views[k].classList.toggle("is-active", k === name);
      }
    }
  }

  for (var ti = 0; ti < tabs.length; ti++) {
    tabs[ti].addEventListener("click", function () {
      showView(this.getAttribute("data-view"));
    });
  }

  // ----- Detail panel (shared across views) -----
  var panel = document.createElement("aside");
  panel.className = "detail-panel";
  panel.setAttribute("aria-hidden", "true");
  var closeBtn = document.createElement("button");
  closeBtn.className = "detail-panel-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.textContent = "\\u00d7";
  var panelBody = document.createElement("div");
  panelBody.className = "detail-panel-body";
  panel.appendChild(closeBtn);
  panel.appendChild(panelBody);
  document.body.appendChild(panel);

  function closePanel() {
    panel.classList.remove("is-open");
    panel.setAttribute("aria-hidden", "true");
    panelBody.innerHTML = "";
    highlightIncident(null);
  }
  closeBtn.addEventListener("click", closePanel);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closePanel(); }
  });

  function openDetail(number) {
    var src = document.querySelector('[data-detail="' + number + '"]');
    if (!src) { return; }
    panelBody.innerHTML = src.innerHTML;
    panel.classList.add("is-open");
    panel.setAttribute("aria-hidden", "false");
    highlightIncident(number);
    wirePanelChips();
  }

  function wirePanelChips() {
    var jumps = panelBody.querySelectorAll("[data-jump]");
    for (var i = 0; i < jumps.length; i++) {
      jumps[i].addEventListener("click", function () {
        jumpToNode(this.getAttribute("data-jump"));
      });
    }
    var qlinks = panelBody.querySelectorAll("[data-q-link]");
    for (var j = 0; j < qlinks.length; j++) {
      qlinks[j].addEventListener("click", function (e) {
        e.preventDefault();
        var qid = this.getAttribute("data-q-link");
        var el = document.getElementById("question-" + qid);
        if (el) {
          if (el.open === false) { el.open = true; }
          el.scrollIntoView({ block: "center" });
          el.classList.add("flash");
          setTimeout(function () { el.classList.remove("flash"); }, 1200);
        }
        closePanel();
      });
    }
  }

  // ----- Graph canvas (Graph view): pan, zoom, search centering -----
  var svg = document.getElementById("graph-svg");
  var fitBox = null;
  var view = null;
  if (svg) {
    var parts = svg.getAttribute("data-fit").split(" ");
    fitBox = { x: +parts[0], y: +parts[1], w: +parts[2], h: +parts[3] };
    view = { x: fitBox.x, y: fitBox.y, w: fitBox.w, h: fitBox.h };
    applyView();

    svg.addEventListener("wheel", function (e) {
      e.preventDefault();
      var rect = svg.getBoundingClientRect();
      var px = (e.clientX - rect.left) / rect.width;
      var py = (e.clientY - rect.top) / rect.height;
      // World point under the cursor stays fixed as we scale toward it.
      var wx = view.x + px * view.w;
      var wy = view.y + py * view.h;
      var factor = e.deltaY < 0 ? 0.85 : 1.18;
      var newW = Math.min(fitBox.w * 2.5, Math.max(fitBox.w * 0.06, view.w * factor));
      var newH = newW * (fitBox.h / fitBox.w);
      view.x = wx - px * newW;
      view.y = wy - py * newH;
      view.w = newW;
      view.h = newH;
      applyView();
    }, { passive: false });

    var dragging = false;
    var lastX = 0;
    var lastY = 0;
    svg.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".gnode")) { return; }
      dragging = true;
      lastX = e.clientX;
      lastY = e.clientY;
      svg.classList.add("is-panning");
      svg.setPointerCapture(e.pointerId);
    });
    svg.addEventListener("pointermove", function (e) {
      if (!dragging) { return; }
      var rect = svg.getBoundingClientRect();
      view.x -= ((e.clientX - lastX) / rect.width) * view.w;
      view.y -= ((e.clientY - lastY) / rect.height) * view.h;
      lastX = e.clientX;
      lastY = e.clientY;
      applyView();
    });
    function endDrag(e) {
      dragging = false;
      svg.classList.remove("is-panning");
      if (e && e.pointerId !== undefined && svg.hasPointerCapture(e.pointerId)) {
        svg.releasePointerCapture(e.pointerId);
      }
    }
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);

    // Hovering a node brightens its incident edges and its label.
    svg.addEventListener("mouseover", function (e) {
      var n = e.target.closest(".gnode");
      if (n) { highlightIncident(n.getAttribute("data-number")); }
    });
    svg.addEventListener("mouseout", function (e) {
      var n = e.target.closest(".gnode");
      if (n && !panel.classList.contains("is-open")) { highlightIncident(null); }
    });
  }

  function applyView() {
    if (!svg) { return; }
    svg.setAttribute(
      "viewBox",
      view.x.toFixed(1) + " " + view.y.toFixed(1) + " " +
      view.w.toFixed(1) + " " + view.h.toFixed(1)
    );
  }

  function centerOnNode(number, zoom) {
    if (!svg) { return false; }
    var node = svg.querySelector('.gnode[data-number="' + number + '"]');
    if (!node) { return false; }
    var cx = +node.getAttribute("cx");
    var cy = +node.getAttribute("cy");
    if (zoom) {
      view.w = fitBox.w * 0.22;
      view.h = fitBox.h * 0.22;
    }
    view.x = cx - view.w / 2;
    view.y = cy - view.h / 2;
    applyView();
    return true;
  }

  function highlightIncident(number) {
    var edges = document.querySelectorAll(".cite-edge, .sup-edge");
    for (var i = 0; i < edges.length; i++) {
      var ed = edges[i];
      var on = number !== null &&
        (ed.getAttribute("data-from") === String(number) ||
         ed.getAttribute("data-to") === String(number));
      ed.classList.toggle("incident", on);
    }
  }

  function flashGraphNode(number) {
    var node = svg && svg.querySelector('.gnode[data-number="' + number + '"]');
    if (!node) { return false; }
    node.classList.add("flash");
    setTimeout(function () { node.classList.remove("flash"); }, 1400);
    return true;
  }

  // A relation chip targets the Graph node first (center + flash), and only
  // falls back to the Browse detail panel if the Graph somehow lacks the node.
  // This is the fix for chips dead-ending on isolated citation targets.
  function jumpToNode(number) {
    closePanel();
    if (svg && svg.querySelector('.gnode[data-number="' + number + '"]')) {
      showView("graph");
      centerOnNode(number, true);
      highlightIncident(number);
      flashGraphNode(number);
      return;
    }
    // Fallback: open the detail panel for the node directly.
    openDetail(number);
  }

  function clearDateHits() {
    if (!svg) { return; }
    var hits = svg.querySelectorAll(".gnode.date-hit");
    for (var i = 0; i < hits.length; i++) { hits[i].classList.remove("date-hit"); }
  }

  // Highlight every Graph node sharing a date, staying in the Graph view. The
  // canvas frames the first such node so the cluster is in view.
  function highlightDate(date) {
    if (!svg) { return; }
    showView("graph");
    clearDateHits();
    var gnodes = svg.querySelectorAll(".gnode[data-number]");
    var first = null;
    for (var i = 0; i < gnodes.length; i++) {
      if (gnodes[i].getAttribute("data-date") === date) {
        gnodes[i].classList.add("date-hit");
        if (first === null) { first = gnodes[i].getAttribute("data-number"); }
      }
    }
    if (first !== null) { centerOnNode(first, false); }
  }

  // Run a story-strip action: center a node, open its detail, or date-highlight.
  function runStory(el) {
    var action = el.getAttribute("data-story");
    if (action === "date") {
      highlightDate(el.getAttribute("data-story-date"));
      return;
    }
    var number = el.getAttribute("data-story-target");
    if (action === "detail") {
      showView("graph");
      centerOnNode(number, true);
      flashGraphNode(number);
      openDetail(number);
    } else {
      jumpToNode(number);
    }
  }

  // ----- Global click / key handlers -----
  document.addEventListener("click", function (e) {
    var trigger = e.target.closest("[data-detail-trigger]");
    if (trigger) {
      openDetail(trigger.getAttribute("data-detail-trigger"));
    }
    var qbadge = e.target.closest("[data-q-badge]");
    if (qbadge) {
      var qs = document.getElementById("questions");
      if (qs) { qs.scrollIntoView({ block: "start" }); }
    }
    var qref = e.target.closest(".q-ref[data-jump]");
    if (qref) {
      e.preventDefault();
      jumpToNode(qref.getAttribute("data-jump"));
    }
    var story = e.target.closest("[data-story]");
    if (story) {
      e.preventDefault();
      runStory(story);
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") { return; }
    var trigger = e.target.closest && e.target.closest("[data-detail-trigger]");
    if (trigger) {
      e.preventDefault();
      openDetail(trigger.getAttribute("data-detail-trigger"));
    }
  });

  // ----- Cross-view search and filters -----
  var search = document.getElementById("search-box");
  var fStatus = document.getElementById("filter-status");
  var fCategory = document.getElementById("filter-category");
  var fConfidence = document.getElementById("filter-confidence");

  function criteria() {
    return {
      needle: (search && search.value || "").trim().toLowerCase(),
      status: fStatus ? fStatus.value : "all",
      category: fCategory ? fCategory.value : "all",
      confidence: fConfidence ? fConfidence.value : "all"
    };
  }

  function filterActive(c) {
    return c.needle !== "" || c.status !== "all" ||
      c.category !== "all" || c.confidence !== "all";
  }

  function matches(c, title, num, status, category, confidence) {
    var matchSearch = c.needle === "" ||
      title.indexOf(c.needle) !== -1 ||
      ("d" + num).indexOf(c.needle) !== -1;
    var matchStatus = c.status === "all" || status === c.status;
    var matchCat = c.category === "all" || category === c.category;
    var matchConf = c.confidence === "all" || confidence === c.confidence;
    return matchSearch && matchStatus && matchCat && matchConf;
  }

  function applyBrowse(c) {
    var cards = document.querySelectorAll(".card[data-number]");
    var anyVisible = false;
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var show = matches(
        c,
        (card.getAttribute("data-title") || "").toLowerCase(),
        card.getAttribute("data-number"),
        card.getAttribute("data-status"),
        card.getAttribute("data-category"),
        card.getAttribute("data-confidence")
      );
      card.style.display = show ? "" : "none";
      if (show) { anyVisible = true; }
    }
    var groups = document.querySelectorAll(".view-browse .category");
    for (var g = 0; g < groups.length; g++) {
      var grpCards = groups[g].querySelectorAll(".card[data-number]");
      var visibleActive = 0;
      var visibleSuperseded = 0;
      for (var h = 0; h < grpCards.length; h++) {
        if (grpCards[h].style.display === "none") { continue; }
        if (grpCards[h].getAttribute("data-status") === "superseded") {
          visibleSuperseded++;
        } else {
          visibleActive++;
        }
      }
      var visible = visibleActive + visibleSuperseded;
      groups[g].style.display = visible > 0 ? "" : "none";
      // Rewrite the count badge to the filtered counts so it stays truthful.
      var badge = groups[g].querySelector(".category-count");
      if (badge) {
        if (visibleSuperseded > 0 && visibleActive > 0) {
          badge.textContent = visibleActive + " active \\u00b7 " +
            visibleSuperseded + " superseded";
        } else if (visibleSuperseded > 0) {
          badge.textContent = visibleSuperseded + " superseded";
        } else {
          badge.textContent = visibleActive + " active";
        }
      }
    }
    var emptyMsg = document.querySelector(".view-browse .empty-filter");
    if (emptyMsg) { emptyMsg.hidden = anyVisible; }
  }

  function applyGraph(c) {
    if (!svg) { return; }
    var active = filterActive(c);
    if (active) { clearDateHits(); }
    var gnodes = svg.querySelectorAll(".gnode[data-number]");
    var matchSet = {};
    var topMatch = null;
    for (var i = 0; i < gnodes.length; i++) {
      var node = gnodes[i];
      var num = node.getAttribute("data-number");
      var ok = matches(
        c,
        (node.getAttribute("data-title") || "").toLowerCase(),
        num,
        node.getAttribute("data-status"),
        node.getAttribute("data-category"),
        node.getAttribute("data-confidence")
      );
      node.classList.toggle("match", active && ok);
      node.classList.toggle("dim", active && !ok);
      if (ok) { matchSet[num] = true; }
      var label = svg.querySelector('.gnode-label[data-label-for="' + num + '"]');
      if (label) {
        // A matching node always shows its label; otherwise the default hub
        // labels show only when no search is active.
        label.classList.toggle("show", active && ok);
        label.classList.toggle("dim", active && !ok);
      }
      if (active && ok && c.needle !== "" && topMatch === null) {
        // First match in document order, biased to an exact D-number hit.
        topMatch = num;
      }
      if (active && ok && ("d" + num) === c.needle) {
        topMatch = num;
      }
    }
    // Focus mode: when a filter is active, dim every edge not touching a match
    // so the readable web is just the matches' connections. Supersession edges
    // stay heavier than citation edges in both states (their base widths differ
    // and edge-dim lowers opacity, not width). Without a filter, restore the
    // faint always-on web.
    var edges = svg.querySelectorAll(".cite-edge, .sup-edge");
    for (var e = 0; e < edges.length; e++) {
      var ed = edges[e];
      if (!active) {
        ed.classList.remove("edge-dim");
        continue;
      }
      var touches = matchSet[ed.getAttribute("data-from")] ||
        matchSet[ed.getAttribute("data-to")];
      ed.classList.toggle("edge-dim", !touches);
    }
    if (topMatch !== null) {
      centerOnNode(topMatch, true);
      highlightIncident(topMatch);
    } else if (!active) {
      highlightIncident(null);
    }
  }

  function applyTimeline(c) {
    // The status filter (and the other facets) hide non-matching timeline marks
    // so the Timeline view is truthful under the selected status too.
    var marks = document.querySelectorAll(".tl-mark[data-number]");
    for (var i = 0; i < marks.length; i++) {
      var m = marks[i];
      var ok = matches(
        c,
        (m.getAttribute("data-title") || "").toLowerCase(),
        m.getAttribute("data-number"),
        m.getAttribute("data-status"),
        m.getAttribute("data-category"),
        m.getAttribute("data-confidence")
      );
      m.style.display = ok ? "" : "none";
    }
  }

  function applyFilters() {
    var c = criteria();
    applyBrowse(c);
    applyGraph(c);
    applyTimeline(c);
  }

  if (search) { search.addEventListener("input", applyFilters); }
  if (fStatus) { fStatus.addEventListener("change", applyFilters); }
  if (fCategory) { fCategory.addEventListener("change", applyFilters); }
  if (fConfidence) { fConfidence.addEventListener("change", applyFilters); }

  // Sync the initial DOM to the controls so what is shown matches the selects.
  applyFilters();
})();
"""


_DOCUMENT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>{styles}</style>
</head>
<body>
{body}
{footer}
<!--graph-payload-start-->
<script id="graph-payload" type="application/json">{payload_json}</script>
<!--graph-payload-end-->
<script>{script}</script>
</body>
</html>
"""
