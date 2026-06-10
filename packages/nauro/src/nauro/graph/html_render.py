"""Render a decision-graph payload into one self-contained HTML document.

The output is a single read-only file: inline CSS, inline vanilla JS, no
external network requests, no third-party assets. The payload is embedded once
as a ``<script type="application/json">`` block; the page reads it at load time
to drive the client-side title filter and the citation-layer toggle.

The page draws a lane-and-thread timeline grouped by decision type, per-thread
lineage cards, and the filtered open-questions list. Supersession relationships
render textually on each card and lineage row ("supersedes D2, D7" / "superseded
by D10") with branch points flagged; there are no drawn connectors in this
version.

This module deliberately consumes only the payload dict, never a store or any
I/O, so relocating it (for example to a hosted renderer that builds the same
payload) is mechanical.

Built with f-strings and string templates only; no Jinja2, no regex. All
authored copy is neutral and diagnostic, uses "decision" as the framing noun,
and carries no em-dashes.
"""

from __future__ import annotations

import json
from html import escape as _html_escape

from nauro_core.decision_model import DECISION_TYPE_VALUES

# Display cap for a node title in the timeline and lineage cards. The full
# title always survives in the embedded payload and in the title attribute; the
# cap only governs the visible label so a very long title cannot overflow.
_TITLE_DISPLAY_CAP = 64

# Lane order for the timeline, derived from the canonical decision-type values
# so a newly added type gets its own lane automatically rather than falling
# silently into "other". Decisions with a null or unrecognized type still land
# in the trailing "other" lane so no decision is dropped from the render.
_LANE_ORDER = list(DECISION_TYPE_VALUES)
_OTHER_LANE = "other"

# Lane keys that read better with a custom label than the generic
# underscore-to-space transform.
_LANE_LABEL_OVERRIDES = {"api_design": "API design"}


def render_html(payload: dict, *, generated_at: str) -> str:
    """Render the graph payload to a complete HTML document.

    Args:
        payload: The dict returned by ``build_graph_payload``. Embedded verbatim
            and also read on the Python side to build the static markup.
        generated_at: A human-readable generation timestamp for the footer. The
            payload itself carries no timestamp (the builder is pure), so the
            caller stamps the time here.

    Returns:
        One self-contained HTML document as a string.
    """
    project = payload.get("project") or "this project"
    decision_count = payload.get("decision_count", 0)
    nodes = payload.get("nodes", [])

    embedded = _embed_payload(payload)
    title_text = _esc(f"Nauro decision graph: {project}")
    footer = _render_footer(project, decision_count, generated_at)

    if not nodes:
        # A store can have zero decisions yet still carry flagged open
        # questions, so the empty branch renders questions too.
        body = _render_empty_state() + _render_open_questions(payload)
    else:
        relations = _supersession_relations(payload)
        body = (
            _render_controls()
            + _render_timeline(payload, relations)
            + _render_lineage(payload, relations)
            + _render_open_questions(payload)
        )

    return _DOCUMENT_TEMPLATE.format(
        title=title_text,
        styles=_STYLES,
        body=body,
        footer=footer,
        payload_json=embedded,
        script=_SCRIPT,
    )


def _embed_payload(payload: dict) -> str:
    """Serialize the payload for a ``<script type="application/json">`` block.

    The ``.replace`` chain below is the only thing that prevents a string value
    (a title containing ``</script>``, say) from terminating the script element
    early: it rewrites ``<``, ``>`` and ``&`` to their JSON unicode escapes,
    which are valid only inside JSON string literals, which is the only place
    those characters occur in this document. Do not remove it on the assumption
    that ``ensure_ascii`` covers the breakout; it does not. ``ensure_ascii`` only
    forces non-ASCII to ``\\uXXXX`` and leaves ``<`` and ``>`` intact.
    """
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _esc(text: str) -> str:
    """Escape text for HTML markup or a double-quoted attribute value.

    ``html.escape`` with ``quote=True`` escapes both quote styles, so this is
    safe in attribute values regardless of the surrounding quote character.
    """
    return _html_escape(text, quote=True)


def _truncate_display(title: str) -> str:
    """Truncate a title at a word boundary with an ellipsis for display.

    The full title is never lost: it stays in the embedded payload and in the
    ``title`` attribute. This governs only the visible label. The cut falls on
    the last whitespace at or before the cap so a word is not split; if the
    first word already exceeds the cap, a hard cut is taken.
    """
    if len(title) <= _TITLE_DISPLAY_CAP:
        return title
    window = title[:_TITLE_DISPLAY_CAP]
    cut = window.rfind(" ")
    if cut <= 0:
        cut = _TITLE_DISPLAY_CAP
    return title[:cut].rstrip() + "…"


def _lane_of(decision_type) -> str:
    """Map a node's decision_type onto a timeline lane key."""
    if decision_type in _LANE_ORDER:
        return decision_type
    return _OTHER_LANE


def _lane_label(lane: str) -> str:
    """Human label for a lane key."""
    return _LANE_LABEL_OVERRIDES.get(lane, lane.replace("_", " "))


def _supersession_relations(payload: dict) -> dict[int, dict[str, list[int]]]:
    """Map each node to the decisions it supersedes and is superseded by.

    An edge ``(from, to)`` means ``from`` supersedes ``to``. For each node this
    returns ``{"supersedes": [...], "superseded_by": [...]}`` with both lists
    ascending. Edges come straight from the payload, so the textual relations on
    the cards reflect exactly the edges the components encode.
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


def _relation_text(relation: dict[str, list[int]] | None) -> str:
    """Render the supersession relations for a node as escaped HTML, or empty.

    Produces lines like "supersedes D2, D7" and "superseded by D10". Returns an
    empty string when the node has no supersession relations.
    """
    if not relation:
        return ""
    parts: list[str] = []
    if relation["supersedes"]:
        refs = ", ".join(f"D{n}" for n in relation["supersedes"])
        parts.append(f'<span class="rel-supersedes">supersedes {refs}</span>')
    if relation["superseded_by"]:
        refs = ", ".join(f"D{n}" for n in relation["superseded_by"])
        parts.append(f'<span class="rel-superseded">superseded by {refs}</span>')
    if not parts:
        return ""
    return f'<span class="node-rel">{"".join(parts)}</span>'


def _render_controls() -> str:
    """Render the filter input and the citation-layer toggle (default off)."""
    return (
        '<section class="controls">'
        '<label class="filter">Filter by title'
        '<input id="title-filter" type="text" autocomplete="off" '
        'placeholder="substring" /></label>'
        '<label class="toggle">'
        '<input id="citation-toggle" type="checkbox" /> Show citation edges'
        "</label>"
        "</section>"
    )


def _render_timeline(payload: dict, relations: dict[int, dict[str, list[int]]]) -> str:
    """Render the lane-and-thread timeline as positioned HTML nodes.

    Lanes group nodes by decision_type. Within a lane, nodes are ordered by date
    then number. Each node card states its supersession relations textually so a
    one-to-many fan is legible from the cards; branch points are flagged.
    """
    nodes = payload.get("nodes", [])
    by_lane: dict[str, list[dict]] = {}
    for node in nodes:
        lane = _lane_of(node.get("decision_type"))
        by_lane.setdefault(lane, []).append(node)

    lane_keys = [k for k in _LANE_ORDER if k in by_lane]
    if _OTHER_LANE in by_lane:
        lane_keys.append(_OTHER_LANE)

    branch_points: set[int] = set()
    for component in payload.get("components", []):
        for num in component.get("branch_points", []):
            branch_points.add(num)

    lanes_html: list[str] = []
    for lane in lane_keys:
        lane_nodes = sorted(by_lane[lane], key=lambda n: (n.get("date", ""), n.get("number", 0)))
        cards = "".join(
            _render_node_card(n, n["number"] in branch_points, relations.get(n["number"]))
            for n in lane_nodes
        )
        lanes_html.append(
            f'<div class="lane"><h3 class="lane-name">{_esc(_lane_label(lane))}'
            f'</h3><div class="lane-track">{cards}</div></div>'
        )

    return (
        '<section class="timeline"><h2>Timeline</h2>'
        '<p class="section-note">Lanes group decisions by type. Each card names the '
        "decisions it supersedes or is superseded by; branch points are flagged. "
        "Toggle citation edges above to list body references.</p>"
        f"{''.join(lanes_html)}</section>"
    )


def _render_node_card(
    node: dict, is_branch_point: bool, relation: dict[str, list[int]] | None
) -> str:
    """Render one decision as a timeline node card."""
    number = node.get("number", 0)
    full_title = node.get("title", "")
    display = _truncate_display(full_title)
    status = node.get("status", "active")
    date = node.get("date", "")
    confidence = node.get("confidence", "")

    classes = ["node", f"status-{_esc(status)}"]
    if is_branch_point:
        classes.append("branch-point")

    title_attr = _esc(full_title)
    return (
        f'<article class="{" ".join(classes)}" data-number="{number}" '
        f'data-title="{title_attr}" title="{title_attr}">'
        f'<span class="node-id">D{number}</span>'
        f'<span class="node-title">{_esc(display)}</span>'
        f'<span class="node-meta">{_esc(date)} · {_esc(confidence)}</span>'
        f'<span class="node-status">{_esc(status)}</span>'
        f"{_relation_text(relation)}"
        "</article>"
    )


def _render_lineage(payload: dict, relations: dict[int, dict[str, list[int]]]) -> str:
    """Render one lineage card per supersession component, oldest-first.

    A component lists every decision in its connected supersession thread,
    ascending by number (which is oldest-first for sequentially numbered
    decisions), with each entry's title, date, status, and its supersession
    relations. Branch points are flagged so a fan is legible.
    """
    components = payload.get("components", [])
    if not components:
        return (
            '<section class="lineage"><h2>Lineage</h2>'
            '<p class="section-note">No supersession threads yet. Every decision '
            "stands on its own.</p></section>"
        )

    node_by_number = {n["number"]: n for n in payload.get("nodes", [])}
    cards: list[str] = []
    for component in components:
        branch_points = set(component.get("branch_points", []))
        rows: list[str] = []
        for num in component.get("nodes", []):
            node = node_by_number.get(num)
            if node is None:
                continue
            full_title = node.get("title", "")
            marker = '<span class="branch-flag">branch</span>' if num in branch_points else ""
            rows.append(
                '<li class="lineage-row" '
                f'data-title="{_esc(full_title)}">'
                f'<span class="node-id">D{num}</span>'
                f'<span class="lineage-title" title="{_esc(full_title)}">'
                f"{_esc(_truncate_display(full_title))}</span>"
                f'<span class="node-meta">{_esc(node.get("date", ""))}</span>'
                f'<span class="node-status">{_esc(node.get("status", ""))}</span>'
                f"{_relation_text(relations.get(num))}"
                f"{marker}</li>"
            )
        size = len(component.get("nodes", []))
        cards.append(
            f'<article class="lineage-card"><h3>Thread of {size} decisions</h3>'
            f'<ul class="lineage-list">{"".join(rows)}</ul></article>'
        )

    return f'<section class="lineage"><h2>Lineage</h2>{"".join(cards)}</section>'


def _render_open_questions(payload: dict) -> str:
    """Render the filtered open-questions list."""
    questions = payload.get("open_questions", [])
    if not questions:
        return (
            '<section class="questions"><h2>Open questions</h2>'
            '<p class="section-note">No open questions.</p></section>'
        )

    items: list[str] = []
    for q in questions:
        qid = _esc(q.get("id", ""))
        body = _esc(q.get("body", ""))
        items.append(f'<li><span class="q-id">{qid}</span><span class="q-body">{body}</span></li>')

    return (
        '<section class="questions"><h2>Open questions</h2>'
        f'<ul class="question-list">{"".join(items)}</ul></section>'
    )


def _render_empty_state() -> str:
    """Render the intentional empty state for a store with no decisions."""
    return (
        '<section class="empty-state">'
        "<h2>No decisions yet</h2>"
        "<p>Record the first decision with <code>nauro note</code> or your "
        "agent's propose-decision tool, then run this command again.</p>"
        "</section>"
    )


def _render_footer(project: str, decision_count: int, generated_at: str) -> str:
    """Render the footer naming the project, decision count, and generation time."""
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
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #14181b;
    --ink: #e7e2d6;
    --navy: #4fb6c4;
    --accent: #c8745f;
    --line: #2a3036;
    --muted: #9a978c;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 1.5rem 4rem;
  background: var(--paper);
  color: var(--ink);
  font-family: Georgia, "Times New Roman", serif;
  line-height: 1.5;
}
header.page-header {
  border-bottom: 2px solid var(--navy);
  padding: 1.5rem 0 1rem;
  margin-bottom: 1.5rem;
}
header.page-header h1 {
  margin: 0;
  font-size: 1.6rem;
  color: var(--navy);
  font-weight: 600;
}
h2 {
  color: var(--navy);
  font-size: 1.2rem;
  border-bottom: 1px solid var(--line);
  padding-bottom: 0.3rem;
}
h3 { font-size: 1rem; margin: 0.6rem 0 0.4rem; }
.section-note { color: var(--muted); font-size: 0.85rem; max-width: 60ch; }
.controls {
  display: flex;
  gap: 1.5rem;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 1.5rem;
  font-family: -apple-system, "Segoe UI", sans-serif;
  font-size: 0.9rem;
}
.controls input[type="text"] {
  margin-left: 0.5rem;
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--ink);
  border-radius: 3px;
}
.lane { margin-bottom: 1.2rem; }
.lane-name {
  color: var(--accent);
  margin: 0 0 0.4rem;
  font-size: 0.95rem;
}
.lane-track {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
}
.node {
  display: flex;
  flex-direction: column;
  min-width: 12rem;
  max-width: 16rem;
  padding: 0.5rem 0.6rem;
  border: 1px solid var(--line);
  border-left: 3px solid var(--navy);
  background: var(--paper);
  font-family: -apple-system, "Segoe UI", sans-serif;
  font-size: 0.82rem;
}
.node.status-superseded { border-left-color: var(--accent); opacity: 0.85; }
.node.branch-point { border-style: dashed; }
.node-id { font-weight: 700; color: var(--navy); }
.node.status-superseded .node-id { color: var(--accent); }
.node-title { margin: 0.15rem 0; }
.node-meta { color: var(--muted); font-size: 0.75rem; }
.node-status { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; }
.node-rel { margin-top: 0.2rem; font-size: 0.74rem; display: flex; flex-direction: column; }
.rel-superseded { color: var(--accent); }
.rel-supersedes { color: var(--muted); }
.lineage-card {
  border: 1px solid var(--line);
  padding: 0.6rem 0.8rem;
  margin-bottom: 1rem;
}
.lineage-list { list-style: none; margin: 0; padding: 0; }
.lineage-row {
  display: flex;
  gap: 0.6rem;
  align-items: baseline;
  flex-wrap: wrap;
  padding: 0.2rem 0;
  border-bottom: 1px solid var(--line);
  font-family: -apple-system, "Segoe UI", sans-serif;
  font-size: 0.82rem;
}
.lineage-row .node-rel { flex-direction: row; gap: 0.6rem; }
.lineage-title { flex: 1; }
.branch-flag {
  color: var(--accent);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.question-list { list-style: none; padding: 0; }
.question-list li {
  display: flex;
  gap: 0.6rem;
  padding: 0.3rem 0;
  border-bottom: 1px solid var(--line);
}
.q-id { font-weight: 700; color: var(--navy); }
.empty-state { padding: 3rem 0; max-width: 50ch; }
.empty-state h2 { border: none; }
code {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  background: var(--line);
  padding: 0.05rem 0.3rem;
  border-radius: 3px;
  font-size: 0.85em;
}
.page-footer {
  margin-top: 3rem;
  padding-top: 1rem;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 0.8rem;
}
.dimmed { opacity: 0.25; }
.citation-edges { display: none; color: var(--muted); font-size: 0.8rem; margin-top: 1rem; }
.citation-edges.visible { display: block; }
@media (hover: hover) {
  .node:hover { border-left-width: 5px; }
}
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
"""


_SCRIPT = """
(function () {
  var raw = document.getElementById("graph-payload").textContent;
  var payload = JSON.parse(raw);

  var filterInput = document.getElementById("title-filter");
  if (filterInput) {
    filterInput.addEventListener("input", function () {
      var needle = filterInput.value.trim().toLowerCase();
      var targets = document.querySelectorAll("[data-title]");
      for (var i = 0; i < targets.length; i++) {
        var el = targets[i];
        var hay = (el.getAttribute("data-title") || "").toLowerCase();
        if (needle === "" || hay.indexOf(needle) !== -1) {
          el.classList.remove("dimmed");
        } else {
          el.classList.add("dimmed");
        }
      }
    });
  }

  var toggle = document.getElementById("citation-toggle");
  var citationBox = document.getElementById("citation-edges");
  if (toggle && citationBox) {
    var syncCitations = function () {
      if (toggle.checked) {
        citationBox.classList.add("visible");
      } else {
        citationBox.classList.remove("visible");
      }
    };
    toggle.addEventListener("change", syncCitations);
    // Firefox restores a checkbox's prior checked state into a fresh DOM on
    // reload; sync the layer to the box once at load so the two never desync.
    syncCitations();
    var edges = payload.citation_edges || [];
    if (edges.length === 0) {
      citationBox.textContent = "No citation edges in this store.";
    } else {
      var parts = [];
      for (var j = 0; j < edges.length; j++) {
        parts.push("D" + edges[j].from + " cites D" + edges[j].to);
      }
      citationBox.textContent = parts.join("  |  ");
    }
  }
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
<header class="page-header"><h1>{title}</h1></header>
{body}
<div id="citation-edges" class="citation-edges"></div>
{footer}
<!--graph-payload-start-->
<script id="graph-payload" type="application/json">{payload_json}</script>
<!--graph-payload-end-->
<script>{script}</script>
</body>
</html>
"""
