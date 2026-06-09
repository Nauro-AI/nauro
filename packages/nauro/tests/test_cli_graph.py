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


def test_supersession_relations_render_textually(tmp_path, monkeypatch, _no_browser):
    """Each card states the decisions it supersedes or is superseded by."""
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # D4 retires D2 and D3 (a fan), so its card lists both targets.
    assert "supersedes D2, D3" in html
    # D2 and D3 each state they were superseded by D4.
    assert "superseded by D4" in html


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


def test_citation_toggle_defaults_off_and_color_scheme_present(tmp_path, monkeypatch, _no_browser):
    """The citation checkbox ships unchecked and both color schemes are styled."""
    store = _populated_store(tmp_path, monkeypatch)

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # The toggle ships unchecked: the rendered input carries no checked attr.
    assert '<input id="citation-toggle" type="checkbox" />' in html
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


def test_long_title_full_in_payload_truncated_in_display(tmp_path, monkeypatch, _no_browser):
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

    # The visible label is truncated with an ellipsis; the 400-x run is not in
    # the rendered node-title span.
    title_span_open = '<span class="node-title">'
    span_start = html.index(title_span_open) + len(title_span_open)
    span_end = html.index("</span>", span_start)
    visible = html[span_start:span_end]
    assert "…" in visible
    assert "x" * 400 not in visible


def test_script_breakout_in_title_and_question_is_escaped(tmp_path, monkeypatch, _no_browser):
    """A decision title and an open question carrying a script-closing tag plus
    quotes and angle brackets must not break out of the embedded JSON or the
    markup, and the payload must still parse. This is the load-critical pin.
    """
    store = _new_store(tmp_path, monkeypatch)
    hostile = 'Title </script><script>alert("x")</script> & <b>bold</b> "quote\''
    _write_decision(store, 2, "hostile", _decision_md(2, hostile))
    (store / OPEN_QUESTIONS_MD).write_text(
        '# Open Questions\n\n- [Q1] Body </script><img src=x onerror="y"> & "quote\'.\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    html = (store / "nauro-graph.html").read_text(encoding="utf-8")

    # Exactly two real script elements close: the JSON block and the behavior
    # script. A breakout would add a third.
    assert html.count("</script>") == 2
    # The injected raw tags never reach the document as live markup.
    assert '<script>alert("x")</script>' not in html
    assert '<img src=x onerror="y">' not in html

    # The payload still parses and round-trips the hostile strings verbatim.
    payload = _read_embedded_payload(html)
    node = next(n for n in payload["nodes"] if n["number"] == 2)
    assert node["title"] == hostile
    assert payload["open_questions"][0]["body"].startswith("Body </script>")
