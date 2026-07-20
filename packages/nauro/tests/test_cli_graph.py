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
from nauro_core.decision_model import Decision, format_decision
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import DECISIONS_DIR, OPEN_QUESTIONS_MD
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import write_decision_file

runner = CliRunner()


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
    """Return canonical decision markdown via the shared serializer.

    Builds the canonical v2 markdown the parser accepts by going through
    ``format_decision`` rather than hand-rolling YAML, so the fixture cannot
    drift from the on-disk format the model defines. The few tests that need a
    deliberately malformed file build the raw bytes inline instead.
    """
    decision = Decision(
        date=date,
        confidence=confidence,
        version=1,
        status=status,
        decision_type=decision_type,
        reversibility="moderate",
        source="manual",
        files_affected=[],
        supersedes=supersedes,
        superseded_by=superseded_by,
        num=num,
        title=title,
        rationale=body,
    )
    return format_decision(decision)


def _new_store(tmp_path, monkeypatch, name: str = "graphproj") -> Path:
    """Register and scaffold a project, chdir into its repo, return the store."""
    _pid, store = register_project_v2(name, [tmp_path])
    scaffold_project_store(name, store)
    monkeypatch.chdir(tmp_path)
    return store


def _populated_store(tmp_path, monkeypatch) -> Path:
    """A store with a supersession thread, a citation, and an open question."""
    store = _new_store(tmp_path, monkeypatch)
    write_decision_file(
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
    write_decision_file(
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
    write_decision_file(
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


def test_happy_path_writes_to_store_dir(tmp_path, monkeypatch):
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


def test_supersession_relations_render_as_drawn_svg_edges(tmp_path, monkeypatch):
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


def test_detail_panel_lists_relations_and_questions(tmp_path, monkeypatch):
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


def test_output_override_writes_there(tmp_path, monkeypatch):
    _populated_store(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere" / "decision-graph.html"

    result = runner.invoke(app, ["graph", "--output", str(target)])
    assert result.exit_code == 0
    assert target.exists()
    assert str(target.resolve()) in result.output
    # Default location must not also be written when overridden.
    store_default = next((tmp_path).glob("**/nauro-graph.html"), None)
    assert store_default is None


def test_output_into_existing_directory(tmp_path, monkeypatch):
    """--output naming a directory writes the default filename inside it."""
    _populated_store(tmp_path, monkeypatch)
    target_dir = tmp_path / "reports"
    target_dir.mkdir()

    result = runner.invoke(app, ["graph", "--output", str(target_dir)])
    assert result.exit_code == 0
    written = target_dir / "nauro-graph.html"
    assert written.exists()
    assert str(written.resolve()) in result.output


def test_output_uses_lf_newlines(tmp_path, monkeypatch):
    """The HTML is written with LF endings so its sha is platform-stable."""
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    raw = (store / "nauro-graph.html").read_bytes()
    assert b"\r\n" not in raw


def test_output_is_self_contained(tmp_path, monkeypatch):
    """No resource sink (src/href/url()/@import) outside the embedded payload.

    A blanket ``http(s)://`` substring scan would false-fail on an inert URL
    sitting in a decision title; the meaningful invariant is that the document
    loads no external resource, so the scan targets actual resource sinks in the
    markup outside the JSON block.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # Put a URL in a decision title to prove the scan tolerates inert text.
    write_decision_file(
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


def test_default_view_is_graph_with_color_schemes(tmp_path, monkeypatch):
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


def test_api_design_lane_label(tmp_path, monkeypatch):
    """The api_design lane renders as 'API design', not 'Api Design'."""
    store = _new_store(tmp_path, monkeypatch)
    write_decision_file(store, 2, "rest", _decision_md(2, "Use REST", decision_type="api_design"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert "API design" in html


def test_unknown_project_exits_1(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    result = runner.invoke(app, ["graph", "--project", "does-not-exist"])
    assert result.exit_code == 1


@pytest.mark.parametrize("clear_seed", [False, True], ids=["scaffold-only", "empty-dir"])
def test_no_renderable_decisions_renders_empty_state(tmp_path, monkeypatch, clear_seed):
    """Both a fresh store (scaffold seed only, excluded) and a genuinely empty
    decisions directory render the same empty UI with zero payload nodes."""
    store = _new_store(tmp_path, monkeypatch)
    if clear_seed:
        for f in (store / DECISIONS_DIR).glob("*.md"):
            f.unlink()

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert "No decisions yet" in html
    payload = _read_embedded_payload(html)
    assert payload["nodes"] == []


def test_empty_state_still_shows_open_questions(tmp_path, monkeypatch):
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


def test_browser_open_failure_prints_hint(tmp_path, monkeypatch):
    """On a headless host where webbrowser.open returns False, hint the path."""
    store = _populated_store(tmp_path, monkeypatch)
    monkeypatch.setattr("nauro.cli.commands.graph.webbrowser.open", lambda *a, **k: False)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    out_path = (store / "nauro-graph.html").resolve()
    assert "Could not open a browser" in result.output
    assert str(out_path) in result.output


def test_malformed_decision_is_skipped_with_warning(tmp_path, monkeypatch):
    store = _populated_store(tmp_path, monkeypatch)
    # A file that the strict parser rejects (missing required frontmatter).
    write_decision_file(
        store, 9, "broken", "---\nnot: valid frontmatter for a decision\n---\n\nbody\n"
    )

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


def test_browse_and_timeline_fail_loud_on_missing_number():
    """A node missing the guaranteed ``number`` key raises in Browse and Timeline.

    The payload builder always sets ``number``; the views hard-index it so a
    malformed payload fails loud the same way the Graph view already does,
    rather than silently rendering a placeholder. Both view renderers are
    exercised directly with a hand-built bad payload.
    """
    from nauro.graph.html_render import _render_browse_view, _render_timeline_view

    bad_node = {
        "title": "No number here",
        "status": "active",
        "decision_type": "architecture",
        "confidence": "high",
        "date": "2026-03-15",
    }
    payload = {"nodes": [bad_node], "components": [], "questions": []}

    with pytest.raises(KeyError):
        _render_browse_view(payload, {}, {})
    with pytest.raises(KeyError):
        _render_timeline_view(payload)


def test_unreadable_decision_file_does_not_abort(tmp_path, monkeypatch):
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


def test_long_title_wraps_in_full_in_browse_view(tmp_path, monkeypatch):
    """Browse cards render the full title with no truncation; titles wrap.

    The v1 timeline truncated the visible label with an ellipsis. The Browse
    view shows the full title (CSS handles wrapping), so the complete 400-x run
    appears verbatim inside the card-title span.
    """
    store = _new_store(tmp_path, monkeypatch)
    long_title = "Adopt the layered ingestion pipeline " + "x" * 400
    write_decision_file(store, 2, "long-title", _decision_md(2, long_title))

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


def test_script_breakout_in_title_question_and_body_is_escaped(tmp_path, monkeypatch):
    """Hostile content in a title, an open-question body, and a decision body
    (carried only under --include-bodies) must not break out of the embedded
    JSON or the markup, and the payload must still parse. This is the
    load-critical pin; every new payload field rendered into markup is covered.
    """
    store = _new_store(tmp_path, monkeypatch)
    hostile_title = 'Title </script><script>alert("x")</script> & <b>bold</b> "quote\''
    hostile_body = 'Body </script><script>alert("b")</script> & <i>i</i> "q\' angle <z>'
    write_decision_file(store, 2, "hostile", _decision_md(2, hostile_title, body=hostile_body))
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
    write_decision_file(
        store,
        10,
        "consolidate",
        _decision_md(10, "Consolidate the cluster", supersedes=str(targets[0]), date="2026-04-10"),
    )
    for i, num in enumerate(targets):
        write_decision_file(
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


def test_fan_in_draws_one_edge_path_per_retired_decision(tmp_path, monkeypatch):
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
    assert lineage.count('class="lnode status-active consolidation"') == 1

    # Graph: four supersession lines into D10, all carrying the emphasis class.
    assert graph.count('<line class="sup-edge') == 4
    assert graph.count('data-from="10"') == 4
    for target in range(2, 6):
        assert f'data-from="10" data-to="{target}"' in graph
    assert graph.count('class="sup-edge consolidation-edge"') == 4


def test_header_counts_reflect_payload(tmp_path, monkeypatch):
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


def test_question_references_link_both_directions(tmp_path, monkeypatch):
    """A question referencing a decision links out, and that decision badges back.

    Q1 references D2 in its body. The question renders a D2 reference button into
    the detail panel, and D2 (a referenced decision) carries an open-question
    badge linking back to the questions section.
    """
    store = _new_store(tmp_path, monkeypatch)
    write_decision_file(store, 2, "rest", _decision_md(2, "Use REST", date="2026-03-15"))
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


def test_timeline_marks_positioned_by_date_not_index(tmp_path, monkeypatch):
    """Timeline marks are placed by their date's fraction of the span, not order.

    Two decisions a wide date gap apart land far from a third clustered near the
    first. A pure index layout would space all three evenly; a date layout puts
    the cluster close and the outlier far. The earliest mark sits at the left
    gutter and the latest at the right edge of the plot.
    """
    store = _new_store(tmp_path, monkeypatch)
    write_decision_file(store, 2, "early", _decision_md(2, "Early one", date="2026-03-01"))
    write_decision_file(store, 3, "early-two", _decision_md(3, "Early two", date="2026-03-02"))
    write_decision_file(store, 4, "late", _decision_md(4, "Late one", date="2026-06-01"))

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


def test_bodies_default_on_and_no_include_bodies_redacts(tmp_path, monkeypatch):
    """Decision bodies render by default (D331); --no-include-bodies redacts them.

    By default the body key is embedded and the rationale renders as structured
    HTML in the detail panel. --no-include-bodies drops the body key and the
    detail block, leaving the redacted titles-and-metadata artifact.
    """
    store = _new_store(tmp_path, monkeypatch)
    body = "The full rationale text that should appear by default."
    write_decision_file(store, 2, "with-body", _decision_md(2, "A decision", body=body))

    # Default: body embedded and shown as structured detail.
    result = runner.invoke(app, ["graph", "--no-open"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    payload = _read_embedded_payload(html)
    node = next(n for n in payload["nodes"] if n["number"] == 2)
    # The body is the full markdown body, so the rationale lives inside it.
    assert body in node["body"]
    assert "Decision detail</summary>" in html
    assert '<div class="body-md">' in html
    # The rationale renders as structured markup, not raw markdown in a <pre>.
    assert body in html
    assert '<pre class="body-text">' not in html

    # --no-include-bodies: no body embedded, no detail block.
    result = runner.invoke(app, ["graph", "--no-open", "--no-include-bodies"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    payload = _read_embedded_payload(html)
    node = next(n for n in payload["nodes"] if n["number"] == 2)
    assert "body" not in node
    assert "Decision detail</summary>" not in html


# ── Graph view (round 3) ──


def test_graph_has_one_node_element_per_payload_node(tmp_path, monkeypatch):
    """Every payload node renders exactly one Graph circle, keyed by number."""
    store = _populated_store(tmp_path, monkeypatch)
    # Add an isolated decision so the disc path is exercised alongside threads.
    write_decision_file(store, 7, "isolated", _decision_md(7, "Standalone", date="2026-03-20"))

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


def test_graph_layout_is_deterministic(tmp_path, monkeypatch):
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


def test_graph_search_centering_hooks_present(tmp_path, monkeypatch):
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


def test_no_relation_chip_dead_ends(tmp_path, monkeypatch):
    """Every relation chip and question reference targets a node that exists.

    A chip's ``data-jump`` must resolve to a graph node (or, as a fallback the
    script handles, a detail block). This guards the external-review finding that
    chips could dead-end on isolated citation targets.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # D5 cites D2 and D3 in its body but has no supersession edge: an isolated
    # citation source whose chips must still resolve.
    write_decision_file(
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


def test_timeline_same_day_same_lane_marks_stack(tmp_path, monkeypatch):
    """Three decisions on the same date in the same lane get three distinct y."""
    store = _new_store(tmp_path, monkeypatch)
    for num in (2, 3, 4):
        write_decision_file(
            store,
            num,
            f"sameday-{num}",
            _decision_md(num, f"Same day {num}", decision_type="architecture", date="2026-04-10"),
        )
    # A second date so there is a real span and the lane is not degenerate.
    write_decision_file(store, 5, "later", _decision_md(5, "Later", date="2026-05-10"))

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


def test_timeline_uses_exact_calendar_dates(tmp_path, monkeypatch):
    """Marks are placed by exact calendar ordinals across a leap-year boundary.

    Three decisions: 2024-02-28, 2024-02-29 (a real leap day), 2024-03-01. With
    exact date math the leap day sits exactly between the other two; the old
    31-day-month approximation would mis-space them. They live in different lanes
    so lane stacking does not perturb the x positions under test.
    """
    store = _new_store(tmp_path, monkeypatch)
    write_decision_file(store, 2, "feb28", _decision_md(2, "Feb 28", date="2024-02-28"))
    write_decision_file(store, 3, "feb29", _decision_md(3, "Leap day", date="2024-02-29"))
    write_decision_file(store, 4, "mar01", _decision_md(4, "Mar 1", date="2024-03-01"))

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


def test_status_filter_defaults_to_all_and_syncs_on_load(tmp_path, monkeypatch):
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


def test_browse_renders_all_decisions_with_truthful_counts(tmp_path, monkeypatch):
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


def test_question_refs_route_through_jump_to_node(tmp_path, monkeypatch):
    """Question reference buttons go graph-first through jumpToNode, not openDetail.

    The no-dead-end scan covers q-ref targets; this pins that the q-ref click
    handler routes through the same Graph-first helper the relation chips use.
    """
    store = _new_store(tmp_path, monkeypatch)
    write_decision_file(store, 2, "rest", _decision_md(2, "Use REST", date="2026-03-15"))
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


def test_graph_focus_mode_dims_non_incident_edges(tmp_path, monkeypatch):
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


def test_story_strip_renders_deterministic_metrics(tmp_path, monkeypatch):
    """The story strip renders the four jump-capable metric buttons.

    Built deterministically from the payload, renderer-side. The fixture has a
    consolidation (D4 retires D2 and D3), a question hotspot (Q1 references D2),
    a busiest date, and a citation anchor.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # D4 already retires D2 and D3 (consolidation defined at the 2+ threshold).
    # Add an active decision cited in another body so the anchor is defined, and
    # a question that references a decision so the hotspot is defined.
    write_decision_file(
        store,
        7,
        "anchor",
        _decision_md(7, "Anchored choice", date="2026-03-21"),
    )
    write_decision_file(
        store,
        8,
        "citer",
        _decision_md(8, "Cites the anchor", date="2026-03-22", body="Builds on D7."),
    )
    # Two decisions on one date so the recent-activity cluster is defined (2+).
    write_decision_file(store, 9, "busy-a", _decision_md(9, "Busy day A", date="2026-03-25"))
    write_decision_file(store, 10, "busy-b", _decision_md(10, "Busy day B", date="2026-03-25"))
    (store / OPEN_QUESTIONS_MD).write_text(
        "# Open Questions\n\n- [Q1] Should the gateway in D4 expose subscriptions.\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    assert '<div class="story-strip"' in graph
    assert "Largest consolidation" in graph
    assert "Open-question hotspot" in graph
    # The busiest-day metric is the highest-count date, named distinctly from the
    # header's latest-decision date.
    assert "Busiest day" in graph
    assert "Recent activity" not in graph
    assert "Anchor" in graph
    # The insight strip is rendered as connected chips, not boxed cards.
    assert 'class="story-chip' in graph
    assert "story-card" not in graph
    # No chip ships selected: the default view is even-emphasis, selection is
    # applied by clicking a chip. Every chip ships unpressed for assistive tech.
    assert "story-chip is-selected" not in graph
    assert 'data-selected="true"' not in graph
    assert 'aria-pressed="false"' in graph
    assert 'aria-pressed="true"' not in graph
    # The strip sits above the canvas in document order.
    assert graph.index('<div class="story-strip"') < graph.index('<div class="graph-canvas">')
    # Chips carry jump actions the script wires up.
    assert 'data-story="center"' in graph
    assert 'data-story="detail"' in graph
    assert 'data-story="date"' in graph
    assert "function runStory(" in html
    assert "function highlightDate(" in html
    # Anchor copy is neutral (cited N times), no semantic claim word.
    assert "cited" in graph


def test_story_strip_omits_undefined_metrics(tmp_path, monkeypatch):
    """A store with no edges and no questions shows no story strip.

    Each metric is undefined (no consolidation, no hotspot, no anchor; a lone
    decision is not a "busiest day" at the 2+ threshold), so the strip renders
    nothing rather than empty buttons.
    """
    store = _new_store(tmp_path, monkeypatch)
    write_decision_file(store, 2, "lonely", _decision_md(2, "A single decision", date="2026-03-15"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")
    assert '<div class="story-strip"' not in html


def test_timeline_single_day_shows_one_tick(tmp_path, monkeypatch):
    """An all-same-day store draws one centered date tick, never a second date.

    The old span = max(last - first, 1) invented a fake next-day tick. With every
    decision on one date, exactly one tick at that date renders and the marks
    center on it.
    """
    store = _new_store(tmp_path, monkeypatch)
    for num in (2, 3):
        write_decision_file(
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


# ── Visual polish (guided spotlight, insight labels, packing priority) ──


def test_default_state_is_even_emphasis(tmp_path, monkeypatch):
    """The default view ships with even emphasis: no spotlight, no selected chip.

    The spotlight is purely click-driven. On load no chip carries the selected
    state and no node or edge carries a spotlight or recede class, so the whole
    graph shows at even weight until an insight chip is clicked.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    # No chip ships selected.
    assert 'data-selected="true"' not in graph
    assert "story-chip is-selected" not in graph
    # No node or edge ships spotlit or receded.
    assert "spotlight" not in graph
    assert " recede" not in graph
    assert 'class="gnode recede"' not in graph


def test_insight_labels_pinned_to_insight_nodes_only(tmp_path, monkeypatch):
    """Pinned insight labels render for exactly the insight target nodes.

    The labels are non-interactive (pointer-events: none in CSS so they never
    block a node click). The named top story gets the stronger at-rest form
    (is-primary), the others the compact form. This is informational labelling,
    not a selection state.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # Add an anchor (cited active decision) and a question hotspot so multiple
    # insight targets exist alongside the consolidation D4.
    write_decision_file(store, 7, "anchor", _decision_md(7, "Anchor", date="2026-03-21"))
    write_decision_file(
        store, 8, "citer", _decision_md(8, "Cites anchor", date="2026-03-22", body="On D7.")
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    # The CSS makes the whole insight-label group non-interactive.
    assert ".insight-label { pointer-events: none; }" in html
    # The named top story (consolidation D4) carries the stronger at-rest label.
    assert 'class="insight-label is-primary"' in graph
    label_count = graph.count('class="insight-label')
    # Every insight label keys to a node that exists as a graph circle.
    pos = 0
    targets = []
    while True:
        i = graph.find('data-insight-for="', pos)
        if i == -1:
            break
        start = i + len('data-insight-for="')
        targets.append(int(graph[start : graph.index('"', start)]))
        pos = start
    assert len(targets) == label_count
    for t in targets:
        # Each label group keys to a node that exists as a graph circle: the
        # data-insight-for attribute is the anchor tying a label to its node.
        assert '<g class="insight-label' in graph
        assert f'data-insight-for="{t}"' in graph
        assert f'data-number="{t}" data-title=' in graph


def test_spotlight_hooks_present(tmp_path, monkeypatch):
    """Spotlight/recede classes and the JS that drives them ship in the markup.

    pytest cannot run the browser, so this pins the class-driven hooks: the
    spotlight and recede CSS tiers for nodes and edges, and the JS functions
    that select an insight, spotlight a node, and clear the spotlight.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # Node and edge spotlight/recede tiers are styled.
    assert ".gnode.spotlight" in html
    assert ".gnode.recede" in html
    assert ".sup-edge.spotlight" in html
    assert ".sup-edge.recede" in html
    assert ".cite-edge.recede" in html
    # The JS spotlight machinery is click-driven (no load-time auto-select).
    assert "function spotlightNode(" in html
    assert "function clearSpotlight(" in html
    assert "function selectInsight(" in html
    assert "function setSelectedChip(" in html
    # A chip click runs the story, which selects the insight (spotlight + chip).
    assert "function runStory(" in html
    # Clearing is wired to Escape and an empty-canvas click.
    assert "clearSpotlight();" in html


def test_pan_surface_suppresses_text_selection(tmp_path, monkeypatch):
    """A pan drag must not engage native text selection on the Graph canvas.

    pytest cannot drive the browser, so this pins the two hooks that suppress
    selection on the pan surface only: user-select: none on .graph-canvas (with
    the -webkit- prefix for Safari) and a preventDefault on the empty-canvas pan
    pointer path. The suppression stays scoped to the canvas, so detail-panel and
    Browse text remain selectable.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # The pan surface suppresses native selection, with the Safari prefix.
    assert ".graph-canvas {" in html
    assert "user-select: none;" in html
    assert "-webkit-user-select: none;" in html
    # Pointer events drive the pan, so touch gestures are suppressed on the SVG.
    assert "touch-action: none;" in html
    # The pan pointer path prevents the drag from initiating a selection.
    assert "e.preventDefault();" in html
    # The suppression is scoped to the canvas, not blanket on the document body.
    assert "body {\n  -webkit-user-select: none;" not in html
    assert "body {\n  user-select: none;" not in html


def test_focus_transitions_clear_incident_highlighting(tmp_path, monkeypatch):
    """Every focus transition starts from a clean incident-edge state.

    pytest cannot drive the browser, so this pins the wiring: clearSpotlight
    resets incident edges, the filter path clears them when no single match is
    focused (an active facet filter with no search topMatch), and the date story
    routes through clearSpotlight first. Without these, selecting an insight then
    clearing or filtering leaves stale incident-highlighted edges behind.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # clearSpotlight resets incident edges before deselecting the chip.
    clear_start = html.index("function clearSpotlight(")
    clear_body = html[clear_start : html.index("function setSelectedChip(")]
    assert "highlightIncident(null);" in clear_body
    # The filter path clears incident edges on the no-single-match branch, not
    # only when the filter is fully inactive.
    apply_body = html[html.index("function applyGraph(") : html.index("function applyTimeline(")]
    assert "highlightIncident(topMatch);" in apply_body
    assert "} else {\n      // No single match" in apply_body
    assert "highlightIncident(null);" in apply_body
    # The date story routes through clearSpotlight (which clears incident) before
    # highlighting the date.
    run_body = html[html.index("function runStory(") : html.index("// ----- Global click")]
    assert 'if (action === "date") {' in run_body
    assert "clearSpotlight();" in run_body


def test_hub_label_suppressed_for_insight_labeled_nodes(tmp_path, monkeypatch):
    """A node carrying a pinned insight pill drops its regular hub label.

    Two labels on one node overlap, so the more informative insight pill wins and
    the .gnode-label text element is not emitted for that node. The consolidation
    D4 is both a hub (high degree) and the primary insight target, so it is the
    overlap case: it must carry an insight pill and no hub label.
    """
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    # D4 is an insight target (carries the primary pill) and a hub (is-hub class).
    assert 'data-insight-for="4"' in graph
    assert 'class="gnode status-active is-hub' in graph
    # No regular hub label is emitted for any node that carries an insight pill.
    pos = 0
    insight_nums = []
    while True:
        i = graph.find('data-insight-for="', pos)
        if i == -1:
            break
        start = i + len('data-insight-for="')
        insight_nums.append(graph[start : graph.index('"', start)])
        pos = start
    assert insight_nums  # the fixture defines at least one insight target
    for num in insight_nums:
        assert 'class="gnode-label" x=' not in graph or f'data-label-for="{num}"' not in graph


# ── Hub-label overlap suppression ──
#
# These build the graph payload directly (so node degree and category are under
# the test's control) and exercise the renderer's deterministic suppression pass
# at the function level, the same way the packing-priority test does. A long
# title widens a label's estimated box, and a dense same-category disc places
# two nodes close enough that their wide boxes intersect, which is the live
# overlap the pass exists to resolve.

_LONG_LABEL_TITLE = "Adopt the layered hexagonal service architecture platform wide spanning title"


def _disc_node(num: int, title: str, *, status: str = "active") -> dict:
    """A single isolated-decision node for a directly-built graph payload."""
    return {
        "number": num,
        "title": title,
        "status": status,
        "decision_type": "architecture",
        "confidence": "high",
        "date": "2026-03-15",
    }


def _emitted_label_nums(payload: dict) -> tuple[list[int], list[int]]:
    """Render the node/label layer and return (hub-label nums, insight nums).

    Runs the same layout and node/label render the Graph view uses, then reads
    back which nodes carry a ``gnode-label`` text element and which carry a
    pinned insight pill, so a test can assert exactly which hub labels survived
    the suppression pass.
    """
    from nauro.graph.html_render import (
        _compute_insights,
        _graph_nodes_and_labels,
        _question_reference_map,
        _supersession_relations,
        build_graph_layout,
    )

    relations = _supersession_relations(payload)
    question_refs = _question_reference_map(payload)
    layout = build_graph_layout(payload)
    node_by_number = {n["number"]: n for n in payload["nodes"]}
    insights = _compute_insights(payload, relations, question_refs)
    _, label_layer = _graph_nodes_and_labels(
        payload,
        layout["positions"],
        layout["radii"],
        layout["degree"],
        relations,
        question_refs,
        node_by_number,
        layout["clusters"],
        insights,
    )

    def _nums(attr: str) -> list[int]:
        found: list[int] = []
        pos = 0
        marker = f'{attr}="'
        while True:
            i = label_layer.find(marker, pos)
            if i == -1:
                break
            start = i + len(marker)
            found.append(int(label_layer[start : label_layer.index('"', start)]))
            pos = start
        return sorted(set(found))

    return _nums("data-label-for"), _nums("data-insight-for")


def test_colliding_same_disc_hub_labels_keep_only_the_higher_degree():
    """Two adjacent same-disc hubs whose label boxes collide emit one hub label.

    Eight isolated architecture decisions form one dense sunflower disc. D2 and
    D6 land adjacent and both carry a long title, so their estimated label boxes
    intersect. D2 has the higher degree (it cites two decisions; D6 cites one),
    so the suppression pass keeps D2 and drops D6. The citation targets are
    superseded, so no anchor insight fires and the case is a clean hub-vs-hub
    collision.
    """
    nodes = [
        _disc_node(
            num,
            _LONG_LABEL_TITLE if num in (2, 6) else f"S{num}",
            status="superseded" if num in (7, 8, 9) else "active",
        )
        for num in range(2, 10)
    ]
    payload = {
        "nodes": nodes,
        "supersession_edges": [],
        # D2 cites two decisions (degree 2), D6 cites one (degree 1); the targets
        # are superseded so the most-cited-active anchor metric stays undefined.
        "citation_edges": [
            {"from": 2, "to": 7},
            {"from": 2, "to": 8},
            {"from": 6, "to": 9},
        ],
        "components": [],
        "open_questions": [],
    }

    hub_labels, insight_labels = _emitted_label_nums(payload)
    assert insight_labels == []  # the fixture defines no insight target
    # The higher-degree node of the colliding pair keeps its label; the lower one
    # is suppressed. Every other node, uncolliding, keeps its label.
    assert 2 in hub_labels
    assert 6 not in hub_labels
    assert hub_labels == [2, 3, 4, 5, 7, 8, 9]


def test_insight_label_blocks_colliding_hub_but_is_never_suppressed():
    """An insight pill survives and suppresses a hub whose box collides with it.

    D2 is the most-cited active decision, so it carries the anchor insight pill,
    and it sits adjacent to D6, a long-titled hub. D6's estimated label box
    intersects D2's pill box. The insight pill is exempt from suppression but its
    box still blocks, so D2 keeps its pill and D6's hub label is dropped.
    """
    nodes = [
        _disc_node(
            num,
            _LONG_LABEL_TITLE if num in (2, 6) else f"S{num}",
            status="superseded" if num in (3, 4, 5) else "active",
        )
        for num in range(2, 10)
    ]
    payload = {
        "nodes": nodes,
        "supersession_edges": [],
        # D2 is cited three times by active short nodes, so it is the anchor.
        "citation_edges": [
            {"from": 3, "to": 2},
            {"from": 4, "to": 2},
            {"from": 5, "to": 2},
        ],
        "components": [],
        "open_questions": [],
    }

    hub_labels, insight_labels = _emitted_label_nums(payload)
    # D2 carries the (exempt) insight pill and never a hub label.
    assert insight_labels == [2]
    assert 2 not in hub_labels
    # D6, the hub colliding with D2's pill, is suppressed; the rest keep labels.
    assert 6 not in hub_labels
    assert hub_labels == [3, 4, 5, 7, 8, 9]


def test_suppression_pass_is_deterministic():
    """The same payload yields the same emitted label set on every render."""
    nodes = [
        _disc_node(
            num,
            _LONG_LABEL_TITLE if num in (2, 6) else f"S{num}",
            status="superseded" if num in (7, 8, 9) else "active",
        )
        for num in range(2, 10)
    ]
    payload = {
        "nodes": nodes,
        "supersession_edges": [],
        "citation_edges": [
            {"from": 2, "to": 7},
            {"from": 2, "to": 8},
            {"from": 6, "to": 9},
        ],
        "components": [],
        "open_questions": [],
    }

    first = _emitted_label_nums(payload)
    second = _emitted_label_nums(payload)
    assert first == second


def test_sparse_disc_keeps_every_hub_label():
    """A sparse disc collides nowhere, so the pass suppresses nothing.

    Four short-titled isolated decisions spread far enough apart that no two
    estimated label boxes intersect, so every hub keeps its label and the pass
    is a no-op. This pins the absence of over-suppression.
    """
    nodes = [_disc_node(num, f"Short title {num}") for num in (2, 3, 4, 5)]
    payload = {
        "nodes": nodes,
        "supersession_edges": [],
        "citation_edges": [],
        "components": [],
        "open_questions": [],
    }

    hub_labels, insight_labels = _emitted_label_nums(payload)
    assert insight_labels == []
    assert hub_labels == [2, 3, 4, 5]


def test_story_chip_selection_state_and_aria(tmp_path, monkeypatch):
    """Chips ship unpressed and mirror selection into aria-pressed, date included.

    The date chip gets the same selected treatment as node chips, and selection
    mirrors into aria-pressed on every chip (they are buttons). This pins the
    static defaults and the JS that toggles them.
    """
    store = _populated_store(tmp_path, monkeypatch)
    # Two decisions on one date so the date (busiest-day) chip is defined.
    write_decision_file(store, 9, "busy-a", _decision_md(9, "Busy day A", date="2026-03-25"))
    write_decision_file(store, 10, "busy-b", _decision_md(10, "Busy day B", date="2026-03-25"))

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    # Every chip ships with the unpressed default in the markup.
    assert 'data-story="date"' in graph
    assert 'aria-pressed="false"' in graph
    assert 'aria-pressed="true"' not in graph
    # setSelectedChip mirrors selection into aria-pressed and matches the date
    # chip by its date, not only node chips by target.
    sel_body = html[html.index("function setSelectedChip(") : html.index("function supNeighbours(")]
    assert 'setAttribute("aria-pressed"' in sel_body
    assert "data-story-date" in sel_body
    assert "data-story-target" in sel_body
    # The date story selects the date chip while its highlight is active, and the
    # date highlight clear deselects the date chip (shared lifetime).
    run_body = html[html.index("function runStory(") : html.index("// ----- Global click")]
    assert "setSelectedChip({ date: date });" in run_body
    dates_start = html.index("function clearDateHits(")
    clear_dates = html[dates_start : html.index("// Highlight every Graph node")]
    assert 'data-story="date"' in clear_dates
    assert 'setAttribute("aria-pressed", "false")' in clear_dates


def test_category_labels_still_rendered(tmp_path, monkeypatch):
    """Category disc labels still render with their class after the restyle."""
    store = _new_store(tmp_path, monkeypatch)
    # Several isolated decisions in one category form a labelled disc.
    for num in range(2, 7):
        write_decision_file(
            store,
            num,
            f"iso-{num}",
            _decision_md(num, f"Isolated {num}", decision_type="infrastructure", date="2026-03-15"),
        )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    graph = _view_region(html, "graph")
    assert '<text class="disc-label"' in graph
    assert "infrastructure" in graph
    # The disc label is non-interactive map chrome.
    assert ".disc-label {" in html
    assert "pointer-events: none;" in html


def test_largest_consolidation_cluster_takes_origin(tmp_path, monkeypatch):
    """The largest-consolidation cluster is placed at the canvas origin.

    Even when a category disc out-sizes the consolidation star by radius, the
    named top story takes the origin slot so it sits near the visual center
    rather than being pushed to the edge. Pinned at the layout-function level so
    the deterministic placement is asserted directly.
    """
    from nauro.graph.html_render import _supersession_relations, build_graph_layout

    # A 3-way fan (D100 retires D1/D2/D3) plus a larger isolated category disc
    # (8 nodes) whose bounding radius exceeds the small fan's.
    nodes = []
    edges = []
    for n in (1, 2, 3):
        nodes.append(
            {
                "number": n,
                "title": f"retired {n}",
                "status": "superseded",
                "decision_type": "architecture",
                "confidence": "high",
                "date": "2026-03-10",
            }
        )
        edges.append({"from": 100, "to": n})
    nodes.append(
        {
            "number": 100,
            "title": "consolidator",
            "status": "active",
            "decision_type": "architecture",
            "confidence": "high",
            "date": "2026-03-20",
        }
    )
    for n in range(200, 212):  # 12 isolated infra nodes -> a big disc
        nodes.append(
            {
                "number": n,
                "title": f"iso {n}",
                "status": "active",
                "decision_type": "infrastructure",
                "confidence": "high",
                "date": "2026-03-15",
            }
        )
    payload = {
        "nodes": nodes,
        "supersession_edges": edges,
        "citation_edges": [],
        "components": [{"nodes": [1, 2, 3, 100], "edges": edges, "branch_points": [100]}],
        "open_questions": [],
    }
    relations = _supersession_relations(payload)

    # The disc is larger by radius than the fan.
    no_priority = build_graph_layout(payload)
    fan_cluster = next(c for c in no_priority["clusters"] if 100 in c["local"])
    disc_cluster = next(c for c in no_priority["clusters"] if c["kind"] == "disc")
    assert disc_cluster["radius"] > fan_cluster["radius"]
    # Without priority the larger disc takes origin.
    assert disc_cluster["center"] == (0.0, 0.0)

    # With the consolidation as priority, its cluster takes origin instead.
    consolidation = max(relations, key=lambda num: len(relations[num].get("supersedes", [])))
    assert consolidation == 100
    prioritized = build_graph_layout(payload, priority_center=100)
    fan_cluster = next(c for c in prioritized["clusters"] if 100 in c["local"])
    assert fan_cluster["priority"] is True
    assert fan_cluster["center"] == (0.0, 0.0)
    assert prioritized["positions"][100] == (0.0, 0.0)

    # Determinism: same input renders identical positions.
    assert build_graph_layout(payload, priority_center=100)["positions"] == prioritized["positions"]
