"""nauro graph — render the decision graph to a self-contained HTML file.

Reads the local store, builds the versioned graph payload in nauro-core, and
writes one read-only HTML document with four views (Graph by default, then
Lineage, Timeline, and Browse) and an integrated open-questions list. The output
lands in the store directory by default because the HTML embeds decision titles
and metadata plus open-question summaries; a current-directory default would
invite committing that store extract into a repo. ``--output`` overrides the
location, and ``--open`` (default on) opens the file in a browser.

By default the file carries decision titles and metadata plus open-question
summaries only, no decision bodies. ``--include-bodies`` adds each decision's
full body markdown, surfaced behind an expander in the detail panel.
"""

from __future__ import annotations

import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import typer
from nauro_core import build_graph_payload, parse_decision
from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.decision_model import Decision
from nauro_core.questions import OpenQuestionsFile

from nauro.cli.utils import resolve_target_project
from nauro.constants import DECISIONS_DIR
from nauro.graph import DEFAULT_GRAPH_FILENAME, render_html
from nauro.store._atomic import atomic_write_text
from nauro.store.reader import read_text_lenient


def _read_decisions_lenient(store_path: Path) -> list[Decision]:
    """Parse every decision file, skipping unreadable or malformed ones.

    The kernel's ``parse_all_decisions`` skips parse failures but logs them at
    debug with no per-file user-facing warning, and it reads through the Store
    protocol rather than tolerating on-disk surprises (a subdirectory named
    ``*.md``, a dangling symlink, an unreadable file). The graph command needs
    both a named stderr warning and that I/O tolerance so one bad entry does not
    deny the user the whole graph, so it keeps its own per-file loop with the
    read inside the guard.
    """
    decisions_dir = store_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return []

    decisions: list[Decision] = []
    for f in sorted(decisions_dir.glob("*.md")):
        try:
            content = read_text_lenient(f)
            decisions.append(parse_decision(content, f.name))
        except Exception as exc:
            typer.echo(f"Skipping unreadable decision file {f.name}: {exc}", err=True)
    return decisions


def _read_open_questions(store_path: Path) -> OpenQuestionsFile | None:
    """Parse the open-questions file when present and readable, else None.

    The read and parse sit inside the guard so a directory shadowing the
    open-questions path, or an unparseable file, drops the questions section
    with a warning rather than aborting the whole render.
    """
    questions_path = store_path / OPEN_QUESTIONS_MD
    if not questions_path.exists():
        return None
    try:
        return OpenQuestionsFile.parse(read_text_lenient(questions_path))
    except Exception as exc:
        typer.echo(f"Skipping unreadable open-questions file: {exc}", err=True)
        return None


def _resolve_output_path(output: Path | None, store_path: Path) -> Path:
    """Resolve where the HTML is written.

    With no ``--output`` the file lands at ``<store>/nauro-graph.html``. An
    explicit ``--output`` that names an existing directory writes the default
    filename inside it; otherwise the path is taken as the full file path.
    """
    if output is None:
        return store_path / DEFAULT_GRAPH_FILENAME
    output = output.expanduser()
    if output.is_dir():
        return output / DEFAULT_GRAPH_FILENAME
    return output


def graph(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Write the HTML here instead of the store directory.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the generated file in a browser (default on).",
    ),
    include_bodies: bool = typer.Option(
        False,
        "--include-bodies/--no-include-bodies",
        help="Embed full decision bodies behind an expander in the detail panel.",
    ),
) -> None:
    """Render the project's decision graph to a self-contained HTML file."""
    project_name, store_path = resolve_target_project(project)

    decisions = _read_decisions_lenient(store_path)
    questions = _read_open_questions(store_path)
    payload = build_graph_payload(
        decisions, questions, project=project_name, include_bodies=include_bodies
    )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = render_html(payload, generated_at=generated_at)

    out_path = _resolve_output_path(output, store_path)
    # newline="\n" keeps the file byte-identical across platforms so its sha is
    # stable wherever it is generated.
    atomic_write_text(out_path, html, newline="\n")

    absolute = out_path.resolve()
    typer.echo(str(absolute))

    if open_browser and not webbrowser.open(absolute.as_uri()):
        typer.echo(
            f"Could not open a browser. Open the file directly: {absolute}",
            err=True,
        )
