"""nauro graph — render the decision graph to a self-contained HTML file.

Reads the local store, builds the versioned graph payload in nauro-core, and writes
one read-only HTML document with four views (Graph, Lineage, Timeline, Browse) and
an integrated open-questions list. ``--open`` (default on) opens it in a browser.

The output lands in the store directory by default because the HTML embeds a store
extract — titles, metadata, question summaries, and full bodies unless
``--no-include-bodies`` — and a current-directory default would invite committing
that into a repo. A generation replica renders its installed generation into the
Nauro home instead, keeping its root free of foreign files. ``--output`` overrides.

``--format json`` emits the same payload on stdout instead: no file is written and
no browser opens. It is the machine-read the desktop viewer consumes. ``--output``
is not honored in JSON mode and errors, because JSON is a stdout contract.
"""

from __future__ import annotations

import enum
import json
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer
from nauro_core import build_graph_payload, parse_decision
from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.decision_model import Decision
from nauro_core.operations.decision_lookup import scan_decision_records
from nauro_core.operations.store import Store
from nauro_core.questions import OpenQuestionsFile

from nauro.cli.generation_reads import generation_line, render_installed
from nauro.cli.utils import resolve_target_project
from nauro.constants import DECISIONS_DIR
from nauro.graph import DEFAULT_GRAPH_FILENAME, render_html
from nauro.store._atomic import atomic_write_text
from nauro.store.home import nauro_home
from nauro.store.reader import read_text_lenient


class GraphFormat(str, enum.Enum):
    """Output format for the graph command."""

    html = "html"
    json = "json"


def _read_decisions_lenient(store_path: Path) -> list[Decision]:
    """Parse every decision file, skipping unreadable or malformed ones with a warning.
    The read sits inside the guard, so a subdirectory named ``*.md``, a dangling symlink or
    an unreadable file costs one entry rather than the whole graph.
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
    """Parse the open-questions file when present and readable, else ``None``.
    The read and parse sit inside the guard, so a shadowing directory or an unparseable file
    drops the questions section with a warning rather than aborting the render.
    """
    questions_path = store_path / OPEN_QUESTIONS_MD
    if not questions_path.exists():
        return None
    try:
        content = read_text_lenient(questions_path)
    except OSError as exc:
        typer.echo(f"Skipping unreadable open-questions file: {exc}", err=True)
        return None
    questions, warning = _parse_open_questions(content)
    if warning is not None:
        typer.echo(warning, err=True)
    return questions


def _parse_open_questions(content: str) -> tuple[OpenQuestionsFile | None, str | None]:
    """Parse open-questions content; an unparseable file yields the warning owed instead."""
    try:
        return OpenQuestionsFile.parse(content), None
    except Exception as exc:
        return None, f"Skipping unreadable open-questions file: {exc}"


@dataclass(frozen=True)
class _GraphInputs:
    """What the graph is built from, with the warnings owed for skipped generation files."""

    decisions: list[Decision]
    questions: OpenQuestionsFile | None
    warnings: tuple[str, ...] = ()


def _generation_inputs(store: Store) -> _GraphInputs:
    """Read the installed generation's graph inputs, holding the warnings until authorized."""
    records, failures, _stems = scan_decision_records(store)
    warnings = [
        f"Skipping unreadable decision file {failure.stem}.md: {failure.error}"
        for failure in failures
    ]
    content = store.read_file(OPEN_QUESTIONS_MD)
    questions = None
    if content is not None:
        questions, warning = _parse_open_questions(content)
        if warning is not None:
            warnings.append(warning)
    return _GraphInputs([record.decision for record in records], questions, tuple(warnings))


def _resolve_output_path(output: Path | None, default: Path) -> Path:
    """Resolve where the HTML is written.
    With no ``--output`` the file lands at ``default``. An ``--output`` naming an existing
    directory writes the default filename inside it; otherwise it is the file path.
    """
    if output is None:
        return default
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
        help="Write the HTML here instead of the store directory. "
        "Not valid with --format json (JSON goes to stdout; redirect it to a file).",
    ),
    format: GraphFormat = typer.Option(
        GraphFormat.html,
        "--format",
        help="html writes a self-contained HTML file; json emits the graph "
        "payload to stdout (no file, no browser).",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the generated file in a browser (default on). "
        "Inapplicable and skipped with --format json.",
    ),
    include_bodies: bool = typer.Option(
        True,
        "--include-bodies/--no-include-bodies",
        help="Embed full decision bodies in the detail panel (default on). "
        "Use --no-include-bodies for a redacted titles-and-metadata artifact.",
    ),
) -> None:
    """Render the project's decision graph to a self-contained HTML file, or to
    a JSON document on stdout with --format json."""
    if format is GraphFormat.json and output is not None:
        raise typer.BadParameter(
            "--output is not valid with --format json; JSON is written to stdout, "
            "so redirect it to a file instead.",
            param_hint="--output",
        )

    project_name, store_path = resolve_target_project(project)

    captured = render_installed(store_path, _generation_inputs)
    if captured is None:
        inputs = _GraphInputs(_read_decisions_lenient(store_path), _read_open_questions(store_path))
        default_output = store_path / DEFAULT_GRAPH_FILENAME
    else:
        inputs, identity = captured
        default_output = nauro_home() / f"nauro-graph-{identity.project_id}.html"
        typer.echo(generation_line(identity), err=True)
    for warning in inputs.warnings:
        typer.echo(warning, err=True)
    payload = build_graph_payload(
        inputs.decisions, inputs.questions, project=project_name, include_bodies=include_bodies
    )

    if format is GraphFormat.json:
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = render_html(payload, generated_at=generated_at)

    out_path = _resolve_output_path(output, default_output)
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
