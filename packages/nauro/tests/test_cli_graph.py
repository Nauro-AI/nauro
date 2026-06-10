"""Tests for the ``nauro graph`` command and its HTML output.

Every invocation patches ``webbrowser.open`` so the suite never opens a real
browser; the autouse fixture below records the call so a test can assert it was
or was not invoked, and with which URI.

These tests own the command and renderer behavior only. Builder invariants
(sentence caps, citation-pair grammar, scaffold-seed exclusion) are pinned in
the nauro-core suite and are not re-asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import DECISIONS_DIR, OPEN_QUESTIONS_MD
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

# The H1 separator the decision parser requires between number and title.
_H1_SEP = "—"


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Replace ``webbrowser.open`` with a recorder so no test opens a browser.

    Returns the call-list so a test can assert the file URI it was handed, or
    that it was never called under ``--no-open``. The default return is True
    (a browser opened); a test that needs the headless path overrides it.
    """
    calls: list[str] = []

    def _record(uri, *args, **kwargs):
        calls.append(uri)
        return True

    monkeypatch.setattr("nauro.cli.commands.graph.webbrowser.open", _record)
    return calls


def _decision_md(
    num: int,
    title: str,
    *,
    decision_type: str = "architecture",
    status: str = "active",
    confidence: str = "high",
    date: str = "2026-03-15",
    supersedes: str | None = None,
    superseded_by: str | None = None,
    body: str = "Rationale body for the decision.",
) -> str:
    """Return canonical decision markdown the parser accepts."""
    sup = "null" if supersedes is None else f"'{supersedes}'"
    sup_by = "null" if superseded_by is None else f"'{superseded_by}'"
    return (
        "---\n"
        f"date: {date}\n"
        "version: 1\n"
        f"status: {status}\n"
        f"confidence: {confidence}\n"
        f"decision_type: {decision_type}\n"
        "reversibility: moderate\n"
        "source: manual\n"
        "files_affected: []\n"
        f"supersedes: {sup}\n"
        f"superseded_by: {sup_by}\n"
        "---\n\n"
        f"# {num:03d} {_H1_SEP} {title}\n\n"
        "## Decision\n\n"
        f"{body}\n"
    )


def _write_decision(store: Path, num: int, slug: str, content: str) -> None:
    (store / DECISIONS_DIR / f"{num:03d}-{slug}.md").write_text(content, encoding="utf-8")


def _new_store(tmp_path, monkeypatch, name: str = "graphproj") -> Path:
    """Register and scaffold a project, chdir into its repo, return the store."""
    store = register_project(name, [tmp_path])
    scaffold_project_store(name, store)
    monkeypatch.chdir(tmp_path)
    return store


def _populated_store(tmp_path, monkeypatch) -> Path:
    """A store with a supersession thread, a citation, and an open question."""
    store = _new_store(tmp_path, monkeypatch)
    _write_decision(
        store,
        2,
        "rest-api",
        _decision_md(
            2,
            "Use REST for the public API",
            decision_type="api_design",
            status="superseded",
            superseded_by="4",
            date="2026-03-16",
            body="Pairs with the pagination decision D3 for the read surface.",
        ),
    )
    _write_decision(
        store,
        3,
        "offset-pagination",
        _decision_md(
            3,
            "Paginate with offset and limit",
            decision_type="api_design",
            status="superseded",
            superseded_by="4",
            date="2026-03-17",
        ),
    )
    _write_decision(
        store,
        4,
        "graphql-gateway",
        _decision_md(
            4,
            "Replace the REST surface with a GraphQL gateway",
            decision_type="architecture",
            supersedes="2",
            date="2026-03-18",
            body="Supersedes the REST surface. See D2 for the earlier reasoning.",
        ),
    )
    (store / OPEN_QUESTIONS_MD).write_text(
        "# Open Questions\n\n- [Q1] Should the gateway expose subscriptions.\n",
        encoding="utf-8",
    )
    return store


def _read_embedded_payload(html: str) -> dict:
    """Extract and parse the embedded JSON payload from the rendered HTML.

    Anchors on the explicit ``graph-payload`` sentinel comments rather than the
    first ``</script>`` after the marker, so the helper does not depend on the
    very escaping it would otherwise be used to verify.
    """
    start = html.index("<!--graph-payload-start-->") + len("<!--graph-payload-start-->")
    end = html.index("<!--graph-payload-end-->")
    block = html[start:end]
    open_tag_end = block.index(">") + 1
    close_tag = block.index("</script>")
    return json.loads(block[open_tag_end:close_tag])


def test_happy_path_writes_to_store_dir(tmp_path, monkeypatch, _no_browser):
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0

    out_path = store / "nauro-graph.html"
    assert out_path.exists()
    # The absolute path is printed to stdout.
    assert str(out_path.resolve()) in result.output

    html = out_path.read_text(encoding="utf-8")
    payload = _read_embedded_payload(html)
    numbers = {n["number"] for n in payload["nodes"]}
    assert numbers == {2, 3, 4}
    # The supersession thread lands in one component.
    assert payload["components"]
    component_nodes = set(payload["components"][0]["nodes"])
    assert {2, 3, 4} <= component_nodes
    # Content, not key-presence: the rendered markup carries the node titles.
    assert "Replace the REST surface with a GraphQL gateway" in html
    assert "Thread of 3 decisions" in html
    # The open question renders.
    assert "Should the gateway expose subscriptions." in html


def test_supersession_relations_render_as_drawn_svg_edges(tmp_path, monkeypatch, _no_browser):
    """Supersession is drawn as SVG edge paths in Lineage, not text labels.

    D4 retires D2 and D3 (a two-way fan-in), so the component draws exactly two
    edge paths, each carrying the retirer/retired pair as data attributes. The
    old textual "supersedes D2, D3" card label must be gone.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # Two drawn edges for this fan-in component, no more, no fewer. A two-way
    # fan is below the consolidation threshold, so these stay plain edges (the
    # trailing quote excludes the consolidation-edge variant class). No edge
    # path is promoted to the emphasis class (the class only appears in the
    # stylesheet, never on a <path>).
    assert html.count('<path class="edge"') == 2
    assert '<path class="edge consolidation-edge"' not in html
    # Each edge names its retirer (from) and retired (to) endpoint.
    assert 'data-from="4" data-to="2"' in html
    assert 'data-from="4" data-to="3"' in html
    # The retired decisions are SVG nodes addressable for a detail click.
    assert '<g class="lnode' in html
    # The v1 textual relation labels are gone.
    assert "supersedes D2, D3" not in html
    assert "superseded by D4" not in html


def test_detail_panel_lists_relations_and_questions(tmp_path, monkeypatch, _no_browser):
    """The shared detail block carries relation chips and linked-question links.

    D4 supersedes D2 and D3, so its detail block lists both as Supersedes chips.
    D2 is superseded by D4, so D2's block lists that. The open question Q1 does
    not reference any decision here, so no question link appears for D4.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # One detail block per node, keyed by number.
    assert '<section class="detail" data-detail="4"' in html
    assert '<section class="detail" data-detail="2"' in html
    # Relation chips jump to the lineage node for the related decision.
    assert '<button class="chip" data-jump="2">D2</button>' in html
    assert '<button class="chip" data-jump="3">D3</button>' in html
    # The "Supersedes" and "Superseded by" relation labels are present.
    assert "Supersedes" in html
    assert "Superseded by" in html


def test_output_override_writes_there(tmp_path, monkeypatch, _no_browser):
    _populated_store(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere" / "decision-graph.html"

    result = runner.invoke(app, ["graph", "--output", str(target)])
    assert result.exit_code == 0
    assert target.exists()
    assert str(target.resolve()) in result.output
    # Default location must not also be written when overridden.
    store_default = next((tmp_path).glob("**/nauro-graph.html"), None)
    assert store_default is None


def test_output_into_existing_directory(tmp_path, monkeypatch, _no_browser):
    """--output naming a directory writes the default filename inside it."""
    _populated_store(tmp_path, monkeypatch)
    target_dir = tmp_path / "reports"
    target_dir.mkdir()

    result = runner.invoke(app, ["graph", "--output", str(target_dir)])
    assert result.exit_code == 0
    written = target_dir / "nauro-graph.html"
    assert written.exists()
    assert str(written.resolve()) in result.output


def test_output_uses_lf_newlines(tmp_path, monkeypatch, _no_browser):
    """The HTML is written with LF endings so its sha is platform-stable."""
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    raw = (store / "nauro-graph.html").read_bytes()
    assert b"\r\n" not in raw


def test_output_is_self_contained(tmp_path, monkeypatch, _no_browser):
    """No resource sink (src/href/url()/@import) outside the embedded payload.

    A blanket ``http(s)://`` substring scan would false-fail on an inert URL
    sitting in a decision title; the meaningful invariant is that the document
    loads no external resource, so the scan targets actual resource sinks in the
    markup outside the JSON block.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # Put a URL in a decision title to prove the scan tolerates inert text.
    _write_decision(
        store,
        5,
        "link-in-title",
        _decision_md(5, "Mirror docs at https://example.com/docs", date="2026-03-19"),
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # Strip the embedded JSON block; a URL inside a title is inert data there.
    start = html.index("<!--graph-payload-start-->")
    end = html.index("<!--graph-payload-end-->")
    markup = html[:start] + html[end:]
    for sink in ("src=", "href=", "url(", "@import"):
        assert sink not in markup
    # No distribution-template token leaks anywhere.
    assert "<!-- protocol:" not in html
    # No unrendered f-string field markers from the document template.
    for marker in ("{title}", "{body}", "{styles}", "{footer}", "{payload_json}", "{script}"):
        assert marker not in html


def test_default_view_is_graph_with_color_schemes(tmp_path, monkeypatch, _no_browser):
    """Graph is the default view; Lineage, Timeline, Browse are not active.

    The card browser is demoted to the Browse tab. Graph is the only view marked
    active at load, and its tab is the active tab. Both color schemes stay
    styled (the Graph canvas is dark in both, the header strip theme-aware).
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # Graph is the active view and the only one.
    assert '<main class="view view-graph is-active"' in html
    assert "view-lineage is-active" not in html
    assert "view-timeline is-active" not in html
    assert "view-browse is-active" not in html
    # The Graph tab is the active tab; Browse exists as a demoted tab.
    assert '<button class="view-tab is-active" data-view="graph">' in html
    assert '<button class="view-tab" data-view="browse">Browse</button>' in html
    # The old Doctrine tab name is gone.
    assert 'data-view="doctrine"' not in html
    # Tab order: Graph, Lineage, Timeline, Browse.
    assert (
        html.index('data-view="graph"')
        < html.index('data-view="lineage"')
        < html.index('data-view="timeline"')
        < html.index('data-view="browse"')
    )
    # The v1 citation-edge checkbox toggle is gone.
    assert 'id="citation-toggle"' not in html
    # Light is the base palette; dark is provided via prefers-color-scheme.
    assert "prefers-color-scheme: dark" in html


def test_api_design_lane_label(tmp_path, monkeypatch, _no_browser):
    """The api_design lane renders as 'API design', not 'Api Design'."""
    store = _new_store(tmp_path, monkeypatch)
    _write_decision(store, 2, "rest", _decision_md(2, "Use REST", decision_type="api_design"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert "API design" in html


def test_unknown_project_exits_1(tmp_path, monkeypatch, _no_browser):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    result = runner.invoke(app, ["graph", "--project", "does-not-exist"])
    assert result.exit_code == 1


def test_scaffold_only_store_renders_empty_state(tmp_path, monkeypatch, _no_browser):
    """A fresh store holds only the scaffold seed, which is excluded; empty UI."""
    store = _new_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert "No decisions yet" in html
    payload = _read_embedded_payload(html)
    assert payload["nodes"] == []


def test_empty_store_renders_empty_state(tmp_path, monkeypatch, _no_browser):
    """A store with an empty decisions directory renders the empty state."""
    store = _new_store(tmp_path, monkeypatch)
    # Remove the scaffold seed so the decisions directory is genuinely empty.
    for f in (store / DECISIONS_DIR).glob("*.md"):
        f.unlink()

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert "No decisions yet" in html


def test_empty_state_still_shows_open_questions(tmp_path, monkeypatch, _no_browser):
    """Zero decisions plus a flagged question still renders the question."""
    store = _new_store(tmp_path, monkeypatch)
    for f in (store / DECISIONS_DIR).glob("*.md"):
        f.unlink()
    (store / OPEN_QUESTIONS_MD).write_text(
        "# Open Questions\n\n- [Q7] Which auth provider do we standardize on.\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert "No decisions yet" in html
    assert "Which auth provider do we standardize on." in html


def test_open_by_default_calls_browser_with_output_path(tmp_path, monkeypatch, _no_browser):
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    out_path = store / "nauro-graph.html"
    assert len(_no_browser) == 1
    assert _no_browser[0] == out_path.resolve().as_uri()


def test_no_open_does_not_call_browser(tmp_path, monkeypatch, _no_browser):
    _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph", "--no-open"])
    assert result.exit_code == 0
    assert _no_browser == []


def test_browser_open_failure_prints_hint(tmp_path, monkeypatch, _no_browser):
    """On a headless host where webbrowser.open returns False, hint the path."""
    store = _populated_store(tmp_path, monkeypatch)
    monkeypatch.setattr("nauro.cli.commands.graph.webbrowser.open", lambda *a, **k: False)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    out_path = (store / "nauro-graph.html").resolve()
    assert "Could not open a browser" in result.output
    assert str(out_path) in result.output


def test_malformed_decision_is_skipped_with_warning(tmp_path, monkeypatch, _no_browser):
    store = _populated_store(tmp_path, monkeypatch)
    # A file that the strict parser rejects (missing required frontmatter).
    _write_decision(store, 9, "broken", "---\nnot: valid frontmatter for a decision\n---\n\nbody\n")

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "Skipping unreadable decision file" in result.output
    assert "009-broken.md" in result.output

    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    payload = _read_embedded_payload(html)
    numbers = {n["number"] for n in payload["nodes"]}
    # The good decisions still render; the broken file is absent.
    assert {2, 3, 4} <= numbers
    assert 9 not in numbers


def test_unreadable_decision_file_does_not_abort(tmp_path, monkeypatch, _no_browser):
    """A subdirectory matching ``*.md`` is skipped, not fatal; the graph renders.

    ``glob("*.md")`` matches directories too, and reading one raises ``IsADir``;
    the read sits inside the per-file guard, so the command exits 0 with the good
    decisions rendered and a warning naming the bad entry.
    """
    store = _populated_store(tmp_path, monkeypatch)
    (store / DECISIONS_DIR / "099-a-directory.md").mkdir()

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "Skipping unreadable decision file" in result.output
    assert "099-a-directory.md" in result.output

    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    payload = _read_embedded_payload(html)
    numbers = {n["number"] for n in payload["nodes"]}
    assert {2, 3, 4} <= numbers


def test_long_title_wraps_in_full_in_browse_view(tmp_path, monkeypatch, _no_browser):
    """Browse cards render the full title with no truncation; titles wrap.

    The v1 timeline truncated the visible label with an ellipsis. The Browse
    view shows the full title (CSS handles wrapping), so the complete 400-x run
    appears verbatim inside the card-title span.
    """
    store = _new_store(tmp_path, monkeypatch)
    long_title = "Adopt the layered ingestion pipeline " + "x" * 400
    _write_decision(store, 2, "long-title", _decision_md(2, long_title))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    payload = _read_embedded_payload(html)
    node = next(n for n in payload["nodes"] if n["number"] == 2)
    # Full title survives in the embedded payload.
    assert node["title"] == long_title

    # The Browse card-title span carries the full title with no ellipsis.
    title_span_open = '<span class="card-title">'
    span_start = html.index(title_span_open) + len(title_span_open)
    span_end = html.index("</span>", span_start)
    visible = html[span_start:span_end]
    assert "…" not in visible
    assert "x" * 400 in visible


def test_script_breakout_in_title_question_and_body_is_escaped(tmp_path, monkeypatch, _no_browser):
    """Hostile content in a title, an open-question body, and a decision body
    (carried only under --include-bodies) must not break out of the embedded
    JSON or the markup, and the payload must still parse. This is the
    load-critical pin; every new payload field rendered into markup is covered.
    """
    store = _new_store(tmp_path, monkeypatch)
    hostile_title = 'Title </script><script>alert("x")</script> & <b>bold</b> "quote\''
    hostile_body = 'Body </script><script>alert("b")</script> & <i>i</i> "q\' angle <z>'
    _write_decision(store, 2, "hostile", _decision_md(2, hostile_title, body=hostile_body))
    (store / OPEN_QUESTIONS_MD).write_text(
        '# Open Questions\n\n- [Q1] Body </script><img src=x onerror="y"> & "quote\'.\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["graph", "--include-bodies"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # Exactly two real script elements close: the JSON block and the behavior
    # script. A breakout from any field would add a third.
    assert html.count("</script>") == 2
    # The injected raw tags never reach the document as live markup.
    assert '<script>alert("x")</script>' not in html
    assert '<script>alert("b")</script>' not in html
    assert '<img src=x onerror="y">' not in html

    # The payload still parses and round-trips the hostile strings verbatim,
    # including the decision body now that bodies are embedded. The body is the
    # full markdown body, so the hostile run is carried inside it.
    payload = _read_embedded_payload(html)
    node = next(n for n in payload["nodes"] if n["number"] == 2)
    assert node["title"] == hostile_title
    assert hostile_body in node["body"]
    assert payload["open_questions"][0]["body"].startswith("Body </script>")


def _fan_in_store(tmp_path, monkeypatch, retired: int = 4) -> Path:
    """A store where one active decision retires ``retired`` earlier ones.

    The retirer (D10) carries a forward ``supersedes`` to the first retired
    decision and the rest carry back-only ``superseded_by`` refs, so the
    component is one fan-in converging on D10.
    """
    store = _new_store(tmp_path, monkeypatch)
    targets = list(range(2, 2 + retired))
    _write_decision(
        store,
        10,
        "consolidate",
        _decision_md(10, "Consolidate the cluster", supersedes=str(targets[0]), date="2026-04-10"),
    )
    for i, num in enumerate(targets):
        _write_decision(
            store,
            num,
            f"retired-{num}",
            _decision_md(
                num,
                f"Retired decision {num}",
                status="superseded",
                superseded_by="10",
                date=f"2026-03-{10 + i:02d}",
            ),
        )
    return store


def _view_region(html: str, view: str) -> str:
    """Return the markup of one ``<main class="view view-<view>" ...>`` block.

    Lets a test scope assertions to a single view, so an edge drawn in both the
    Lineage and Graph views is counted in only the intended one.
    """
    anchor = f'data-view="{view}"'
    start = html.index(f'<main class="view view-{view}')
    # The next <main ...> after this one, or end of document, bounds the region.
    nxt = html.find("<main ", start + 1)
    end = nxt if nxt != -1 else len(html)
    region = html[start:end]
    assert anchor in region
    return region


def test_fan_in_draws_one_edge_path_per_retired_decision(tmp_path, monkeypatch, _no_browser):
    """A four-way fan-in draws four edges into the retirer in BOTH Lineage and Graph.

    Every retired decision contributes one drawn edge to D10 in the Lineage DAG
    (SVG paths) and one in the Graph canvas (SVG lines). D10 is an active fan-in
    of four, so it reads as a consolidation: every edge into it carries the
    emphasis class in both views so the converging bundle is the most prominent
    drawn object.
    """
    store = _fan_in_store(tmp_path, monkeypatch, retired=4)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    lineage = _view_region(html, "lineage")
    graph = _view_region(html, "graph")

    # Lineage: four drawn edge paths, all pointing at D10. The class-agnostic
    # count tolerates the consolidation-edge variant class.
    assert lineage.count("<path class=") == 4
    assert lineage.count('data-from="10"') == 4
    for target in range(2, 6):
        assert f'data-from="10" data-to="{target}"' in lineage
    assert lineage.count('class="edge consolidation-edge"') == 4
    assert lineage.count('class="lnode status-active branch consolidation"') == 1
    assert 'data-fanin="4"' in lineage

    # Graph: four supersession lines into D10, all carrying the emphasis class.
    assert graph.count('<line class="sup-edge') == 4
    assert graph.count('data-from="10"') == 4
    for target in range(2, 6):
        assert f'data-from="10" data-to="{target}"' in graph
    assert graph.count('class="sup-edge consolidation-edge"') == 4


def test_header_counts_reflect_payload(tmp_path, monkeypatch, _no_browser):
    """The header strip states active, superseded, and open-question counts."""
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    payload = _read_embedded_payload(html)
    active = sum(1 for n in payload["nodes"] if n["status"] == "active")
    superseded = sum(1 for n in payload["nodes"] if n["status"] == "superseded")
    # The populated store has D4 active, D2 and D3 superseded.
    assert active == 1
    assert superseded == 2
    assert f"<strong>{active}</strong> active" in html
    assert f"<strong>{superseded}</strong> superseded" in html
    # One open question.
    assert "<strong>1</strong> open questions" in html


def test_question_references_link_both_directions(tmp_path, monkeypatch, _no_browser):
    """A question referencing a decision links out, and that decision badges back.

    Q1 references D2 in its body. The question renders a D2 reference button into
    the detail panel, and D2 (a referenced decision) carries an open-question
    badge linking back to the questions section.
    """
    store = _new_store(tmp_path, monkeypatch)
    _write_decision(store, 2, "rest", _decision_md(2, "Use REST", date="2026-03-15"))
    (store / OPEN_QUESTIONS_MD).write_text(
        "# Open Questions\n\n- [Q1] Does the REST surface in D2 need versioning.\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    payload = _read_embedded_payload(html)
    assert payload["open_questions"][0]["references"] == [2]

    # Forward direction: the question lists a clickable D2 reference.
    assert '<button class="q-ref" data-jump="2">D2</button>' in html
    # Reverse direction: D2's Browse card badges one open question.
    assert 'data-q-badge="2"' in html
    assert "1 open question" in html
    # And D2's detail block links the question id back.
    assert 'data-q-link="Q1"' in html


def test_timeline_marks_positioned_by_date_not_index(tmp_path, monkeypatch, _no_browser):
    """Timeline marks are placed by their date's fraction of the span, not order.

    Two decisions a wide date gap apart land far from a third clustered near the
    first. A pure index layout would space all three evenly; a date layout puts
    the cluster close and the outlier far. The earliest mark sits at the left
    gutter and the latest at the right edge of the plot.
    """
    store = _new_store(tmp_path, monkeypatch)
    _write_decision(store, 2, "early", _decision_md(2, "Early one", date="2026-03-01"))
    _write_decision(store, 3, "early-two", _decision_md(3, "Early two", date="2026-03-02"))
    _write_decision(store, 4, "late", _decision_md(4, "Late one", date="2026-06-01"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    cx = {}
    for num in (2, 3, 4):
        token = f'data-number="{num}" data-date='
        # Marks carry cx before data-number in the circle element; find the
        # circle for this number and read its cx attribute.
        anchor = html.index(token)
        seg_start = html.rfind('<circle class="tl-mark', 0, anchor)
        seg = html[seg_start:anchor]
        cx_key = 'cx="'
        cx_pos = seg.index(cx_key) + len(cx_key)
        cx[num] = float(seg[cx_pos : seg.index('"', cx_pos)])

    # A one-day cluster gap is tiny next to the three-month gap to the outlier:
    # the layout spaces by real date, not by even index. An index layout would
    # make both gaps equal.
    cluster_gap = abs(cx[2] - cx[3])
    outlier_gap = cx[4] - cx[3]
    assert outlier_gap > cluster_gap * 20
    # Earliest is at the left edge of the plot; latest at the right edge.
    assert cx[2] == min(cx.values())
    assert cx[4] == max(cx.values())


def test_include_bodies_flag_embeds_and_shows_body(tmp_path, monkeypatch, _no_browser):
    """--include-bodies carries the decision body into the payload and detail panel.

    Without the flag, no body key is embedded and no body expander renders. With
    it, the body is embedded and surfaced behind a collapsed expander.
    """
    store = _new_store(tmp_path, monkeypatch)
    body = "The full rationale text that should only appear under the flag."
    _write_decision(store, 2, "with-body", _decision_md(2, "A decision", body=body))

    # Default: no body embedded, no body expander.
    result = runner.invoke(app, ["graph", "--no-open"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    payload = _read_embedded_payload(html)
    node = next(n for n in payload["nodes"] if n["number"] == 2)
    assert "body" not in node
    assert "Decision body</summary>" not in html

    # With the flag: body embedded and shown behind an expander.
    result = runner.invoke(app, ["graph", "--no-open", "--include-bodies"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    payload = _read_embedded_payload(html)
    node = next(n for n in payload["nodes"] if n["number"] == 2)
    # The body is the full markdown body, so the rationale lives inside it.
    assert body in node["body"]
    assert "Decision body</summary>" in html
    assert body in html


# ── Graph view (round 3) ──


def test_graph_has_one_node_element_per_payload_node(tmp_path, monkeypatch, _no_browser):
    """Every payload node renders exactly one Graph circle, keyed by number."""
    store = _populated_store(tmp_path, monkeypatch)
    # Add an isolated decision so the disc path is exercised alongside threads.
    _write_decision(store, 7, "isolated", _decision_md(7, "Standalone", date="2026-03-20"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    payload = _read_embedded_payload(html)
    graph = _view_region(html, "graph")
    # One circle per node, no more, no fewer.
    assert graph.count('<circle class="gnode') == len(payload["nodes"])
    # Each node number appears exactly once as a graph circle.
    for node in payload["nodes"]:
        assert graph.count(f'data-number="{node["number"]}" data-title=') == 1
    # The graph supersession-edge count equals the payload edge count.
    assert graph.count('<line class="sup-edge') == payload["stats"]["supersession_edge_count"]
    # The citation web line count equals the payload citation-edge count.
    assert graph.count('<line class="cite-edge') == payload["stats"]["citation_edge_count"]


def test_graph_layout_is_deterministic(tmp_path, monkeypatch, _no_browser):
    """Rendering the same store twice yields byte-identical HTML (no randomness)."""
    _populated_store(tmp_path, monkeypatch)

    out_a = tmp_path / "a.html"
    out_b = tmp_path / "b.html"
    r1 = runner.invoke(app, ["graph", "--no-open", "--output", str(out_a)])
    r2 = runner.invoke(app, ["graph", "--no-open", "--output", str(out_b)])
    assert r1.exit_code == 0
    assert r2.exit_code == 0
    # The footer carries a generation timestamp, so strip it before comparing the
    # layout-bearing markup; the rest must be byte-identical across renders.
    a = out_a.read_text(encoding="utf-8")
    b = out_b.read_text(encoding="utf-8")

    def _strip_footer(html: str) -> str:
        i = html.index('<footer class="page-footer">')
        j = html.index("</footer>", i)
        return html[:i] + html[j:]

    assert _strip_footer(a) == _strip_footer(b)


def test_graph_search_centering_hooks_present(tmp_path, monkeypatch, _no_browser):
    """The Graph search-centering machinery and the attributes it needs exist."""
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # The pannable SVG with a fit-to-content baseline viewBox.
    assert 'id="graph-svg"' in html
    assert 'data-fit="' in html
    # The centering and filter functions the search wires up.
    assert "function centerOnNode(" in html
    assert "function applyGraph(" in html
    assert "function highlightIncident(" in html
    # The data attributes the search reads off each graph node.
    graph = _view_region(html, "graph")
    assert "data-title=" in graph
    assert "data-status=" in graph
    assert "data-category=" in graph
    assert "data-confidence=" in graph


def test_no_relation_chip_dead_ends(tmp_path, monkeypatch, _no_browser):
    """Every relation chip and question reference targets a node that exists.

    A chip's ``data-jump`` must resolve to a graph node (or, as a fallback the
    script handles, a detail block). This guards the external-review finding that
    chips could dead-end on isolated citation targets.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # D5 cites D2 and D3 in its body but has no supersession edge: an isolated
    # citation source whose chips must still resolve.
    _write_decision(
        store,
        5,
        "isolated-citer",
        _decision_md(5, "Cites earlier decisions", date="2026-03-20", body="See D2 and D3."),
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    present = set()
    pos = graph.find('data-detail-trigger="')
    while pos != -1:
        start = pos + len('data-detail-trigger="')
        present.add(int(graph[start : graph.index('"', start)]))
        pos = graph.find('data-detail-trigger="', start)

    # Collect every chip / question-ref / q-link target across the document.
    targets: list[int] = []
    for marker in ('data-jump="',):
        pos = html.find(marker)
        while pos != -1:
            start = pos + len(marker)
            targets.append(int(html[start : html.index('"', start)]))
            pos = html.find(marker, start)
    assert targets  # the fixture has relations, so there are chips to check
    # Every chip target is a real graph node, so no chip dead-ends.
    for t in targets:
        assert t in present, f"chip target D{t} has no graph node"


def test_timeline_same_day_same_lane_marks_stack(tmp_path, monkeypatch, _no_browser):
    """Three decisions on the same date in the same lane get three distinct y."""
    store = _new_store(tmp_path, monkeypatch)
    for num in (2, 3, 4):
        _write_decision(
            store,
            num,
            f"sameday-{num}",
            _decision_md(num, f"Same day {num}", decision_type="architecture", date="2026-04-10"),
        )
    # A second date so there is a real span and the lane is not degenerate.
    _write_decision(store, 5, "later", _decision_md(5, "Later", date="2026-05-10"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    timeline = _view_region(html, "timeline")
    ys: list[float] = []
    for num in (2, 3, 4):
        token = f'data-number="{num}" data-date='
        anchor = timeline.index(token)
        seg_start = timeline.rfind('<circle class="tl-mark', 0, anchor)
        seg = timeline[seg_start:anchor]
        cy_pos = seg.index('cy="') + len('cy="')
        ys.append(float(seg[cy_pos : seg.index('"', cy_pos)]))
    # All three share the same date (same cx) but stack to distinct y positions.
    assert len(set(ys)) == 3


def test_timeline_uses_exact_calendar_dates(tmp_path, monkeypatch, _no_browser):
    """Marks are placed by exact calendar ordinals across a leap-year boundary.

    Three decisions: 2024-02-28, 2024-02-29 (a real leap day), 2024-03-01. With
    exact date math the leap day sits exactly between the other two; the old
    31-day-month approximation would mis-space them. They live in different lanes
    so lane stacking does not perturb the x positions under test.
    """
    store = _new_store(tmp_path, monkeypatch)
    _write_decision(store, 2, "feb28", _decision_md(2, "Feb 28", date="2024-02-28"))
    _write_decision(store, 3, "feb29", _decision_md(3, "Leap day", date="2024-02-29"))
    _write_decision(store, 4, "mar01", _decision_md(4, "Mar 1", date="2024-03-01"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    timeline = _view_region(html, "timeline")
    cx = {}
    for num in (2, 3, 4):
        token = f'data-number="{num}" data-date='
        anchor = timeline.index(token)
        seg_start = timeline.rfind('<circle class="tl-mark', 0, anchor)
        seg = timeline[seg_start:anchor]
        cx_pos = seg.index('cx="') + len('cx="')
        cx[num] = float(seg[cx_pos : seg.index('"', cx_pos)])

    # Span is two days (Feb 28 to Mar 1, counting the leap day). The leap day is
    # exactly halfway; equal one-day gaps on each side.
    left_gap = cx[3] - cx[2]
    right_gap = cx[4] - cx[3]
    assert abs(left_gap - right_gap) < 0.5
    assert cx[2] < cx[3] < cx[4]


# ── Pre-ship fixes (round 4) ──


def test_status_filter_defaults_to_all_and_syncs_on_load(tmp_path, monkeypatch, _no_browser):
    """The Status dropdown defaults to All, and the page syncs the DOM on load.

    The control state and the view must not diverge: the first option is All, and
    the script calls applyFilters() once at the end so the initial DOM matches
    the controls (no node starts dimmed under the default All).
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # All is the first option, before Active and Superseded.
    seg_start = html.index('<select id="filter-status">')
    seg = html[seg_start : seg_start + 200]
    assert '<option value="all">All</option>' in seg
    assert seg.index('value="all"') < seg.index('value="active"')
    assert seg.index('value="active"') < seg.index('value="superseded"')
    # The script syncs the DOM to the controls on load.
    assert "applyFilters();" in html
    # Status-based dimming is driven by the matches() facet and the filterActive
    # gate, so selecting Active genuinely narrows every view rather than being a
    # no-op (the round-3 bug where status===active meant "no filter").
    assert "function filterActive(" in html
    assert 'c.status !== "all"' in html


def test_browse_renders_all_decisions_with_truthful_counts(tmp_path, monkeypatch, _no_browser):
    """Browse renders active and superseded cards, with truthful per-group counts.

    The populated store has D4 active plus D2 and D3 superseded, all in the same
    or adjacent categories. Browse must render a card for every decision (not
    active-only), superseded cards visibly distinct, and the per-group count must
    name both active and superseded.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    payload = _read_embedded_payload(html)
    browse = _view_region(html, "browse")
    # One card per decision (active and superseded both rendered).
    assert browse.count('<article class="card status-') == len(payload["nodes"])
    assert browse.count('data-status="superseded"') == sum(
        1 for n in payload["nodes"] if n["status"] == "superseded"
    )
    assert browse.count('data-status="active"') == sum(
        1 for n in payload["nodes"] if n["status"] == "active"
    )
    # Superseded cards carry a visible status mark.
    assert "superseded</span>" in browse
    # At least one group's count names both active and superseded truthfully.
    assert "active · " in browse and " superseded</span>" in browse
    # The count badge carries the data attributes the script rewrites on filter.
    assert 'class="category-count" data-active=' in browse


def test_question_refs_route_through_jump_to_node(tmp_path, monkeypatch, _no_browser):
    """Question reference buttons go graph-first through jumpToNode, not openDetail.

    The no-dead-end scan covers q-ref targets; this pins that the q-ref click
    handler routes through the same Graph-first helper the relation chips use.
    """
    store = _new_store(tmp_path, monkeypatch)
    _write_decision(store, 2, "rest", _decision_md(2, "Use REST", date="2026-03-15"))
    (store / OPEN_QUESTIONS_MD).write_text(
        "# Open Questions\n\n- [Q1] Does the REST surface in D2 need versioning.\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # The q-ref handler calls jumpToNode (graph-first), not openDetail directly.
    assert "jumpToNode(qref.getAttribute" in html
    # Q1 references D2; the q-ref button exists and its target is a graph node.
    payload = _read_embedded_payload(html)
    assert payload["open_questions"][0]["references"] == [2]
    assert '<button class="q-ref" data-jump="2">D2</button>' in html
    graph = _view_region(html, "graph")
    assert 'data-detail-trigger="2"' in graph


def test_graph_focus_mode_dims_non_incident_edges(tmp_path, monkeypatch, _no_browser):
    """Focus mode hooks: edge-dim class and the JS that toggles it on filter.

    pytest cannot run the browser, so this pins the markup and JS wiring: the
    edge-dim class is styled for both edge layers, applyGraph toggles it keyed on
    whether an edge touches a match, and supersession edges stay heavier than
    citation edges in the styled widths.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # Both edge layers have a dim state styled.
    assert ".cite-edge.edge-dim" in html
    assert ".sup-edge.edge-dim" in html
    # applyGraph computes a match set and toggles edge-dim by incidence.
    assert 'ed.classList.toggle("edge-dim"' in html
    assert "matchSet[ed.getAttribute" in html
    # Supersession base stroke stays heavier than citation base stroke.
    sup_w = html.index(".sup-edge { stroke:")
    cite_w = html.index(".cite-edge { stroke:")
    assert "stroke-width: 1.8" in html[sup_w : sup_w + 80]
    assert "stroke-width: 1;" in html[cite_w : cite_w + 80]


def test_story_strip_renders_deterministic_metrics(tmp_path, monkeypatch, _no_browser):
    """The story strip renders the four jump-capable metric buttons.

    Built deterministically from the payload, renderer-side. The fixture has a
    consolidation (D4 retires D2 and D3), a question hotspot (Q1 references D2),
    a busiest date, and a citation anchor.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # D4 already retires D2 and D3 (consolidation defined at the 2+ threshold).
    # Add an active decision cited in another body so the anchor is defined, and
    # a question that references a decision so the hotspot is defined.
    _write_decision(
        store,
        7,
        "anchor",
        _decision_md(7, "Anchored choice", date="2026-03-21"),
    )
    _write_decision(
        store,
        8,
        "citer",
        _decision_md(8, "Cites the anchor", date="2026-03-22", body="Builds on D7."),
    )
    # Two decisions on one date so the recent-activity cluster is defined (2+).
    _write_decision(store, 9, "busy-a", _decision_md(9, "Busy day A", date="2026-03-25"))
    _write_decision(store, 10, "busy-b", _decision_md(10, "Busy day B", date="2026-03-25"))
    (store / OPEN_QUESTIONS_MD).write_text(
        "# Open Questions\n\n- [Q1] Should the gateway in D4 expose subscriptions.\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    assert '<div class="story-strip">' in graph
    assert "Largest consolidation" in graph
    assert "Open-question hotspot" in graph
    assert "Recent activity" in graph
    assert "Anchor" in graph
    # The story strip sits above the canvas in document order.
    assert graph.index('<div class="story-strip">') < graph.index('<div class="graph-canvas">')
    # Buttons carry jump actions the script wires up.
    assert 'data-story="center"' in graph
    assert 'data-story="detail"' in graph
    assert 'data-story="date"' in graph
    assert "function runStory(" in html
    assert "function highlightDate(" in html
    # Anchor copy is neutral (cited N times), no semantic claim word.
    assert "cited" in graph


def test_story_strip_omits_undefined_metrics(tmp_path, monkeypatch, _no_browser):
    """A store with no edges and no questions shows no story strip.

    Each metric is undefined (no consolidation, no hotspot, no anchor; a lone
    decision is not a "recent activity cluster" at the 2+ threshold), so the
    strip renders nothing rather than empty buttons.
    """
    store = _new_store(tmp_path, monkeypatch)
    _write_decision(store, 2, "lonely", _decision_md(2, "A single decision", date="2026-03-15"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert '<div class="story-strip">' not in html


def test_timeline_single_day_shows_one_tick(tmp_path, monkeypatch, _no_browser):
    """An all-same-day store draws one centered date tick, never a second date.

    The old span = max(last - first, 1) invented a fake next-day tick. With every
    decision on one date, exactly one tick at that date renders and the marks
    center on it.
    """
    store = _new_store(tmp_path, monkeypatch)
    for num in (2, 3):
        _write_decision(
            store,
            num,
            f"sameday-{num}",
            _decision_md(num, f"Same day {num}", decision_type="architecture", date="2026-04-10"),
        )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    timeline = _view_region(html, "timeline")
    # Exactly one tick label, and it is the only date in the store.
    assert timeline.count('class="tl-tick-label"') == 1
    assert "2026-04-10" in timeline
    # No invented second date (the next day must not appear).
    assert "2026-04-11" not in timeline
